from __future__ import annotations

from pathlib import Path

import requests

from obsidian_mattermost_notifier.config import MattermostConfig, VaultConfig
from obsidian_mattermost_notifier.mattermost import MattermostClient, MattermostError


class FakeResponse:
    def __init__(self, data: object, error: Exception | None = None) -> None:
        self.data = data
        self.error = error

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error

    def json(self) -> object:
        return self.data


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.verify = True
        self.get_response = FakeResponse({"id": "resolved-channel"})
        self.post_response = FakeResponse({"id": "post-123"})
        self.get_calls: list[tuple[str, float]] = []
        self.post_calls: list[tuple[str, dict[str, str], float]] = []
        self.closed = False

    def get(self, url: str, *, timeout: float) -> FakeResponse:
        self.get_calls.append((url, timeout))
        return self.get_response

    def post(self, url: str, *, json: dict[str, str], timeout: float) -> FakeResponse:
        self.post_calls.append((url, json, timeout))
        return self.post_response

    def close(self) -> None:
        self.closed = True


def vault(*, channel_id: str | None = None) -> VaultConfig:
    return VaultConfig(
        enabled=True,
        vault_path=Path("/vault"),
        vault_name="vault",
        team_name="dev team",
        channel_name="docs/notice",
        channel_id=channel_id,
        ignore_folders=(),
        settle_seconds=0,
    )


def client_with_fake() -> tuple[MattermostClient, FakeSession]:
    session = FakeSession()
    client = MattermostClient(
        MattermostConfig("https://mattermost.example.com", "secret-token", False),
        session=session,
        timeout=7,
    )
    return client, session


def test_resolve_channel_and_post_with_mock_session() -> None:
    client, session = client_with_fake()

    channel_id = client.channel_id_for(vault())
    post_id = client.post_message(channel_id, "test message")

    assert channel_id == "resolved-channel"
    assert post_id == "post-123"
    assert session.get_calls == [
        (
            (
                "https://mattermost.example.com/api/v4/teams/name/dev%20team/"
                "channels/name/docs%2Fnotice"
            ),
            7,
        )
    ]
    assert session.post_calls[0][1] == {
        "channel_id": "resolved-channel",
        "message": "test message",
    }
    assert session.headers["Authorization"] == "Bearer secret-token"
    assert session.verify is False


def test_explicit_channel_id_skips_lookup() -> None:
    client, session = client_with_fake()
    assert client.channel_id_for(vault(channel_id="fixed-channel")) == "fixed-channel"
    assert session.get_calls == []


def test_http_errors_are_safe_mattermost_errors() -> None:
    client, session = client_with_fake()
    session.post_response = FakeResponse(
        {}, requests.HTTPError("401 token=do-not-copy")
    )

    try:
        client.post_message("channel", "body")
    except MattermostError as exc:
        assert str(exc) == "Mattermost 게시 요청에 실패했습니다."
        assert "do-not-copy" not in str(exc)
    else:
        raise AssertionError("MattermostError was not raised")
