from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from obsidian_mattermost_notifier.config import VaultConfig, parse_config
from obsidian_mattermost_notifier.service import NotifierService


class FakePublisher:
    def __init__(self, *, fail_posts: bool = False) -> None:
        self.posts: list[tuple[str, str]] = []
        self.fail_posts = fail_posts
        self.closed = False

    def channel_id_for(self, vault: object) -> str:
        return "channel-1"

    def post_message(self, channel_id: str, message: str) -> str:
        if self.fail_posts:
            raise RuntimeError("temporary outage")
        self.posts.append((channel_id, message))
        return f"post-{len(self.posts)}"

    def close(self) -> None:
        self.closed = True


class FailFirstVaultOncePublisher(FakePublisher):
    def __init__(self) -> None:
        super().__init__()
        self.channel_calls: list[str] = []
        self.failed_once = False

    def channel_id_for(self, vault: VaultConfig) -> str:
        self.channel_calls.append(vault.vault_name)
        if vault.vault_name == "broken" and not self.failed_once:
            self.failed_once = True
            raise RuntimeError("temporary channel lookup failure")
        return f"channel-{vault.vault_name}"


class FakeWatcher:
    instances: ClassVar[list[FakeWatcher]] = []

    def __init__(
        self, vault: object, on_file: object, on_missing: object, **_kwargs: object
    ) -> None:
        self.vault = vault
        self.on_file = on_file
        self.on_missing = on_missing
        self.started = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    @property
    def is_alive(self) -> bool:
        return self.started


def app_config(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return parse_config(
        {
            "mattermost": {
                "url": "https://mattermost.example.com",
                "token": "test-token",
            },
            "obsidian_notifications": [
                {
                    "enabled": True,
                    "vault_path": str(vault),
                    "vault_name": "vault",
                    "channel_id": "channel-1",
                    "settle_seconds": 0,
                }
            ],
            "state": {"database_path": str(tmp_path / "state" / "notifier.db")},
        }
    )


def test_first_start_baselines_and_restart_posts_file_created_while_down(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "obsidian_mattermost_notifier.service.VaultWatcher", FakeWatcher
    )
    config = app_config(tmp_path)
    existing = config.enabled_vaults[0].vault_path / "existing.md"
    existing.write_text("# Existing", encoding="utf-8")

    first_publisher = FakePublisher()
    first = NotifierService(config, publisher=first_publisher)
    first.start()
    assert first_publisher.posts == []
    first.stop()

    new_document = config.enabled_vaults[0].vault_path / "during-stop.md"
    new_document.write_text(
        "---\ntitle: Created while stopped\n---\nsecret body", encoding="utf-8"
    )
    second_publisher = FakePublisher()
    second = NotifierService(config, publisher=second_publisher)
    try:
        second.start()
        assert len(second_publisher.posts) == 1
        _, message = second_publisher.posts[0]
        assert "Created while stopped" in message
        assert "during-stop.md" in message
        assert "secret body" not in message
    finally:
        second.stop()


def test_failed_post_stays_pending_for_next_restart(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "obsidian_mattermost_notifier.service.VaultWatcher", FakeWatcher
    )
    config = app_config(tmp_path)
    baseline = NotifierService(config, publisher=FakePublisher())
    baseline.start()
    baseline.stop()

    document = config.enabled_vaults[0].vault_path / "retry.md"
    document.write_text("# Retry", encoding="utf-8")
    failed = NotifierService(config, publisher=FakePublisher(fail_posts=True))
    failed.start()
    failed.stop()

    recovered_publisher = FakePublisher()
    recovered = NotifierService(config, publisher=recovered_publisher)
    try:
        recovered.start()
        assert len(recovered_publisher.posts) == 1
    finally:
        recovered.stop()


def test_one_vault_failure_does_not_stop_others_and_is_retried(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "obsidian_mattermost_notifier.service.VaultWatcher", FakeWatcher
    )
    broken = tmp_path / "broken"
    healthy = tmp_path / "healthy"
    broken.mkdir()
    healthy.mkdir()
    config = parse_config(
        {
            "mattermost": {
                "url": "https://mattermost.example.com",
                "token": "test-token",
            },
            "obsidian_notifications": [
                {
                    "vault_path": str(broken),
                    "vault_name": "broken",
                    "channel_id": "broken-channel",
                },
                {
                    "vault_path": str(healthy),
                    "vault_name": "healthy",
                    "channel_id": "healthy-channel",
                },
            ],
            "state": {"database_path": str(tmp_path / "state" / "notifier.db")},
        }
    )
    publisher = FailFirstVaultOncePublisher()
    service = NotifierService(config, publisher=publisher)
    try:
        service.start()
        assert publisher.channel_calls == ["broken", "healthy"]

        service.restart_failed_watchers()
        assert publisher.channel_calls == ["broken", "healthy", "broken"]
    finally:
        service.stop()
