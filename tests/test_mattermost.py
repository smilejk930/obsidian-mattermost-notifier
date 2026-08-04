from __future__ import annotations

from pathlib import Path

import pytest
import requests

from obsidian_mattermost_notifier.config import MattermostConfig, VaultConfig
from obsidian_mattermost_notifier.mattermost import (
    MattermostClient,
    MattermostRequestError,
)


class FakeResponse:
    def __init__(
        self,
        data: object,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self.data = data
        self.status_code = status_code
        self.headers = headers or {}
        self.json_error = json_error

    def json(self) -> object:
        if self.json_error:
            raise self.json_error
        return self.data


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.verify = True
        self.get_response = FakeResponse({"id": "resolved-channel"})
        self.post_response = FakeResponse({"id": "post-123"})
        self.get_error: Exception | None = None
        self.post_error: Exception | None = None
        self.get_calls: list[tuple[str, float]] = []
        self.post_calls: list[tuple[str, dict[str, str], float]] = []
        self.closed = False

    def get(self, url: str, *, timeout: float) -> FakeResponse:
        self.get_calls.append((url, timeout))
        if self.get_error:
            raise self.get_error
        return self.get_response

    def post(self, url: str, *, json: dict[str, str], timeout: float) -> FakeResponse:
        self.post_calls.append((url, json, timeout))
        if self.post_error:
            raise self.post_error
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


def test_validate_auth_success_and_failure_are_safe() -> None:
    client, session = client_with_fake()
    session.get_response = FakeResponse({"id": "user-1"})
    client.validate_auth()
    assert session.get_calls[0][0].endswith("/api/v4/users/me")

    session.get_response = FakeResponse(
        {"message": "token=must-not-leak"}, status_code=401
    )
    with pytest.raises(MattermostRequestError) as raised:
        client.validate_auth()
    assert raised.value.code == "http_401"
    assert not raised.value.retryable
    assert "must-not-leak" not in str(raised.value)


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


@pytest.mark.parametrize("status_code", [408, 500, 503])
def test_retryable_http_statuses_are_classified(status_code: int) -> None:
    client, session = client_with_fake()
    session.post_response = FakeResponse({}, status_code=status_code)

    with pytest.raises(MattermostRequestError) as raised:
        client.post_message("channel", "body")
    assert raised.value.retryable
    assert raised.value.code == f"http_{status_code}"


def test_rate_limit_honors_retry_after() -> None:
    client, session = client_with_fake()
    session.post_response = FakeResponse(
        {}, status_code=429, headers={"Retry-After": "17"}
    )

    with pytest.raises(MattermostRequestError) as raised:
        client.post_message("channel", "body")
    assert raised.value.retryable
    assert raised.value.retry_after == 17


def test_timeout_and_connection_error_are_retryable() -> None:
    client, session = client_with_fake()
    for error, code in (
        (requests.Timeout("secret timeout detail"), "timeout"),
        (requests.ConnectionError("secret connection detail"), "connection_error"),
    ):
        session.post_error = error
        with pytest.raises(MattermostRequestError) as raised:
            client.post_message("channel", "body")
        assert raised.value.retryable
        assert raised.value.code == code
        assert "secret" not in str(raised.value)


def test_nonretryable_400_and_invalid_json() -> None:
    client, session = client_with_fake()
    session.post_response = FakeResponse({"token": "hidden"}, status_code=400)
    with pytest.raises(MattermostRequestError) as raised:
        client.post_message("channel", "body")
    assert not raised.value.retryable
    assert raised.value.code == "http_400"
    assert "hidden" not in str(raised.value)

    session.post_response = FakeResponse({}, json_error=ValueError("secret response"))
    with pytest.raises(MattermostRequestError) as raised:
        client.post_message("channel", "body")
    assert not raised.value.retryable
    assert raised.value.code == "invalid_json"
    assert "secret" not in str(raised.value)
