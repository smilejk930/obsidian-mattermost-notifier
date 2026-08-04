from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import quote

import requests

from .config import MattermostConfig, VaultConfig


class MattermostError(RuntimeError):
    """A safe-to-log Mattermost failure."""


class MattermostRequestError(MattermostError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


class ResponseLike(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> Any: ...


class SessionLike(Protocol):
    headers: dict[str, str]
    verify: bool

    def get(self, url: str, *, timeout: float) -> ResponseLike: ...

    def post(
        self, url: str, *, json: dict[str, str], timeout: float
    ) -> ResponseLike: ...

    def close(self) -> None: ...


class MattermostClient:
    def __init__(
        self,
        config: MattermostConfig,
        *,
        session: SessionLike | None = None,
        timeout: float | None = None,
    ) -> None:
        self._base_url = config.url.rstrip("/") + "/api/v4"
        self._headers = {
            "Authorization": f"Bearer {config.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._verify_ssl = config.verify_ssl
        self._provided_session = session
        self._thread_local = threading.local()
        self._sessions: list[SessionLike] = []
        self._sessions_lock = threading.Lock()
        if session is not None:
            self._configure_session(session)
        self._timeout = timeout or config.request_timeout_seconds

    def validate_auth(self) -> None:
        data = self._get_json("/users/me", operation="Mattermost 인증 검증")
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise MattermostRequestError(
                "Mattermost 인증 응답에 사용자 id가 없습니다.",
                code="invalid_auth_response",
                retryable=False,
            )

    def channel_id_for(self, vault: VaultConfig) -> str:
        if vault.channel_id:
            return vault.channel_id
        assert vault.team_name is not None and vault.channel_name is not None
        team = quote(vault.team_name, safe="")
        channel = quote(vault.channel_name, safe="")
        data = self._get_json(
            f"/teams/name/{team}/channels/name/{channel}",
            operation=f"Mattermost 채널 조회 ({vault.team_name}/{vault.channel_name})",
        )
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise MattermostRequestError(
                f"Mattermost 채널 응답에 id가 없습니다: "
                f"{vault.team_name}/{vault.channel_name}",
                code="invalid_channel_response",
                retryable=False,
            )
        return data["id"]

    def post_message(self, channel_id: str, message: str) -> str:
        data = self._post_json(
            "/posts",
            {"channel_id": channel_id, "message": message},
            operation="Mattermost 게시",
        )
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise MattermostRequestError(
                "Mattermost 게시 응답에 post id가 없습니다.",
                code="invalid_post_response",
                retryable=False,
            )
        return data["id"]

    def close(self) -> None:
        if self._provided_session is not None:
            self._provided_session.close()
            return
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()

    def _get_json(self, path: str, *, operation: str) -> dict[str, Any]:
        try:
            response = self._session().get(self._base_url + path, timeout=self._timeout)
        except requests.Timeout as exc:
            raise _network_error(operation, "timeout") from exc
        except requests.ConnectionError as exc:
            raise _network_error(operation, "connection_error") from exc
        except requests.RequestException as exc:
            raise MattermostRequestError(
                f"{operation} 요청 오류가 발생했습니다.",
                code="request_error",
                retryable=False,
            ) from exc
        return _response_json(response, operation=operation)

    def _post_json(
        self, path: str, payload: dict[str, str], *, operation: str
    ) -> dict[str, Any]:
        try:
            response = self._session().post(
                self._base_url + path,
                json=payload,
                timeout=self._timeout,
            )
        except requests.Timeout as exc:
            raise _network_error(operation, "timeout") from exc
        except requests.ConnectionError as exc:
            raise _network_error(operation, "connection_error") from exc
        except requests.RequestException as exc:
            raise MattermostRequestError(
                f"{operation} 요청 오류가 발생했습니다.",
                code="request_error",
                retryable=False,
            ) from exc
        return _response_json(response, operation=operation)

    def _session(self) -> SessionLike:
        if self._provided_session is not None:
            return self._provided_session
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            self._configure_session(session)
            self._thread_local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def _configure_session(self, session: SessionLike) -> None:
        session.headers.update(self._headers)
        session.verify = self._verify_ssl


def _response_json(response: ResponseLike, *, operation: str) -> dict[str, Any]:
    status = response.status_code
    if not 200 <= status < 300:
        retryable = status in {408, 429} or 500 <= status < 600
        retry_after = _retry_after_seconds(response.headers) if status == 429 else None
        raise MattermostRequestError(
            f"{operation} 실패 (HTTP {status}).",
            code=f"http_{status}",
            retryable=retryable,
            status_code=status,
            retry_after=retry_after,
        )
    try:
        data = response.json()
    except (ValueError, TypeError) as exc:
        raise MattermostRequestError(
            f"{operation} 응답 JSON이 올바르지 않습니다.",
            code="invalid_json",
            retryable=False,
            status_code=status,
        ) from exc
    if not isinstance(data, dict):
        raise MattermostRequestError(
            f"{operation} 응답 형식이 올바르지 않습니다.",
            code="invalid_response",
            retryable=False,
            status_code=status,
        )
    return data


def _network_error(operation: str, code: str) -> MattermostRequestError:
    return MattermostRequestError(
        f"{operation} 중 Mattermost 서버에 연결하지 못했습니다.",
        code=code,
        retryable=True,
    )


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    value = next(
        (
            header_value
            for key, header_value in headers.items()
            if key.lower() == "retry-after"
        ),
        None,
    )
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
