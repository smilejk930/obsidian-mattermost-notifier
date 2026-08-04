from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import quote

import requests

from .config import MattermostConfig, VaultConfig


class MattermostError(RuntimeError):
    """A safe-to-log Mattermost request failure."""


class ResponseLike(Protocol):
    def raise_for_status(self) -> None: ...

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
        timeout: float = 10,
    ) -> None:
        self._base_url = config.url.rstrip("/") + "/api/v4"
        self._session: SessionLike = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {config.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self._session.verify = config.verify_ssl
        self._timeout = timeout

    def channel_id_for(self, vault: VaultConfig) -> str:
        if vault.channel_id:
            return vault.channel_id
        assert vault.team_name is not None and vault.channel_name is not None
        team = quote(vault.team_name, safe="")
        channel = quote(vault.channel_name, safe="")
        url = f"{self._base_url}/teams/name/{team}/channels/name/{channel}"
        try:
            response = self._session.get(url, timeout=self._timeout)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise MattermostError(
                f"Mattermost 채널을 조회하지 못했습니다: {vault.team_name}/{vault.channel_name}"
            ) from exc
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("id"), str)
            or not data["id"]
        ):
            raise MattermostError(
                f"Mattermost 채널 응답에 id가 없습니다: {vault.team_name}/{vault.channel_name}"
            )
        return data["id"]

    def post_message(self, channel_id: str, message: str) -> str:
        try:
            response = self._session.post(
                f"{self._base_url}/posts",
                json={"channel_id": channel_id, "message": message},
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise MattermostError("Mattermost 게시 요청에 실패했습니다.") from exc
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("id"), str)
            or not data["id"]
        ):
            raise MattermostError("Mattermost 게시 응답에 post id가 없습니다.")
        return data["id"]

    def close(self) -> None:
        self._session.close()
