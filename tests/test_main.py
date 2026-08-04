from __future__ import annotations

import signal
from pathlib import Path
from typing import ClassVar

import pytest

from obsidian_mattermost_notifier import __main__ as cli
from obsidian_mattermost_notifier.config import parse_config


def app_config(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return parse_config(
        {
            "mattermost": {
                "url": "https://mattermost.example.com",
                "token": "test-token",
            },
            "obsidian_notifications": [
                {
                    "vault_path": str(vault),
                    "vault_name": "example_vault",
                    "channel_id": "channel-1",
                }
            ],
            "state": {"database_path": str(tmp_path / "notifier.db")},
        }
    )


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, _config: object) -> None:
        self.auth_calls = 0
        self.channel_calls = 0
        self.posts: list[tuple[str, str]] = []
        self.closed = False
        self.__class__.instances.append(self)

    def validate_auth(self) -> None:
        self.auth_calls += 1

    def channel_id_for(self, _vault: object) -> str:
        self.channel_calls += 1
        return "channel-1"

    def post_message(self, channel_id: str, message: str) -> str:
        self.posts.append((channel_id, message))
        return "test-post"

    def close(self) -> None:
        self.closed = True


def test_check_does_not_post_and_smoke_test_posts_exactly_once(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "MattermostClient", FakeClient)
    FakeClient.instances.clear()
    config = app_config(tmp_path)

    cli.check_mattermost(config)
    check_client = FakeClient.instances[-1]
    assert check_client.auth_calls == 1
    assert check_client.channel_calls == 1
    assert check_client.posts == []
    assert check_client.closed

    post_id = cli.send_test_message(config, "example_vault")
    smoke_client = FakeClient.instances[-1]
    assert post_id == "test-post"
    assert smoke_client.auth_calls == 1
    assert smoke_client.channel_calls == 1
    assert len(smoke_client.posts) == 1
    assert "--send-test" in smoke_client.posts[0][1]
    assert smoke_client.closed


def test_smoke_test_rejects_unknown_vault_without_creating_client(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "MattermostClient", FakeClient)
    FakeClient.instances.clear()
    with pytest.raises(ValueError, match="활성 보관함"):
        cli.send_test_message(app_config(tmp_path), "missing")
    assert FakeClient.instances == []


def test_sigterm_requests_clean_service_shutdown(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            (
                "mattermost:",
                '  url: "https://mattermost.example.com"',
                '  token: "test-token"',
                "obsidian_notifications:",
                "  - enabled: true",
                f'    vault_path: "{vault}"',
                '    vault_name: "vault"',
                '    channel_id: "channel"',
                "state:",
                f'  database_path: "{tmp_path / "notifier.db"}"',
            )
        ),
        encoding="utf-8",
    )
    calls: list[str] = []
    handlers: dict[int, object] = {}

    class FakeService:
        def __init__(self, _config: object) -> None:
            pass

        def start(self) -> None:
            calls.append("start")

        def restart_failed_watchers(self) -> None:
            calls.append("restart")

        def stop(self) -> None:
            calls.append("stop")

    class FakeEvent:
        def __init__(self) -> None:
            self.is_set = False

        def set(self) -> None:
            self.is_set = True

        def wait(self, timeout: float) -> bool:
            handler = handlers[signal.SIGTERM]
            handler(signal.SIGTERM, None)  # type: ignore[operator]
            return self.is_set

    monkeypatch.setattr(cli, "NotifierService", FakeService)
    monkeypatch.setattr(cli.threading, "Event", FakeEvent)
    monkeypatch.setattr(
        cli.signal,
        "signal",
        lambda signum, handler: handlers.__setitem__(signum, handler),
    )

    assert cli.main(["--config", str(config_path)]) == 0
    assert calls == ["start", "stop"]
