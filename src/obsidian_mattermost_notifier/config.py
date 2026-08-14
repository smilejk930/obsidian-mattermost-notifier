from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import yaml


class ConfigError(ValueError):
    """Raised when the configuration is missing or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class MattermostConfig:
    url: str
    token: str
    verify_ssl: bool = True
    request_timeout_seconds: float = 10.0
    immediate_retry_attempts: int = 3
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 300.0
    retry_jitter_ratio: float = 0.2


@dataclass(frozen=True, slots=True)
class VaultConfig:
    enabled: bool
    vault_path: Path
    vault_name: str
    team_name: str | None
    channel_name: str | None
    channel_id: str | None
    ignore_folders: tuple[str, ...]
    settle_seconds: float
    notification_quiet_seconds: float = 30.0
    draft_name_patterns: tuple[str, ...] = (
        r"무제(?: \d+)?",
        r"Untitled(?: \d+)?",
    )


@dataclass(frozen=True, slots=True)
class StateConfig:
    database_path: Path


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(frozen=True, slots=True)
class AppConfig:
    mattermost: MattermostConfig
    obsidian_notifications: tuple[VaultConfig, ...]
    state: StateConfig
    logging: LoggingConfig

    @property
    def enabled_vaults(self) -> tuple[VaultConfig, ...]:
        return tuple(vault for vault in self.obsidian_notifications if vault.enabled)


def load_config(path: str | Path, *, validate_paths: bool = True) -> AppConfig:
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream) or {}
    except OSError as exc:
        raise ConfigError(
            f"설정 파일을 읽을 수 없습니다: {config_path}: {exc}"
        ) from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"설정 YAML 형식이 올바르지 않습니다: {exc}") from exc
    return parse_config(raw, validate_paths=validate_paths)


def parse_config(raw: object, *, validate_paths: bool = True) -> AppConfig:
    root = _mapping(raw, "config")
    mattermost_raw = _mapping(root.get("mattermost"), "mattermost")

    url = _nonempty_string(mattermost_raw.get("url"), "mattermost.url").rstrip("/")
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ConfigError("mattermost.url은 http 또는 https 절대 URL이어야 합니다.")
    token = _nonempty_string(mattermost_raw.get("token"), "mattermost.token")
    if token == "CHANGE_ME":
        raise ConfigError("mattermost.token의 CHANGE_ME를 실제 토큰으로 바꿔야 합니다.")
    verify_ssl = _boolean(
        mattermost_raw.get("verify_ssl", True), "mattermost.verify_ssl"
    )
    request_timeout_seconds = _positive_number(
        mattermost_raw.get("request_timeout_seconds", 10),
        "mattermost.request_timeout_seconds",
    )
    immediate_retry_attempts = _nonnegative_integer(
        mattermost_raw.get("immediate_retry_attempts", 3),
        "mattermost.immediate_retry_attempts",
    )
    retry_base_seconds = _positive_number(
        mattermost_raw.get("retry_base_seconds", 1),
        "mattermost.retry_base_seconds",
    )
    retry_max_seconds = _positive_number(
        mattermost_raw.get("retry_max_seconds", 300),
        "mattermost.retry_max_seconds",
    )
    if retry_max_seconds < retry_base_seconds:
        raise ConfigError(
            "mattermost.retry_max_seconds는 retry_base_seconds 이상이어야 합니다."
        )
    retry_jitter_ratio = _number_in_range(
        mattermost_raw.get("retry_jitter_ratio", 0.2),
        "mattermost.retry_jitter_ratio",
        minimum=0,
        maximum=1,
    )

    notifications_raw = root.get("obsidian_notifications")
    if not isinstance(notifications_raw, list) or not notifications_raw:
        raise ConfigError(
            "obsidian_notifications는 하나 이상의 항목을 가진 배열이어야 합니다."
        )

    vaults = tuple(
        _parse_vault(index, item, validate_paths=validate_paths)
        for index, item in enumerate(notifications_raw)
    )
    enabled = tuple(vault for vault in vaults if vault.enabled)
    if not enabled:
        raise ConfigError("enabled: true인 보관함이 하나 이상 필요합니다.")

    names: dict[str, int] = {}
    paths: dict[Path, int] = {}
    for index, vault in enumerate(vaults):
        if not vault.enabled:
            continue
        name_key = vault.vault_name.casefold()
        if name_key in names:
            raise ConfigError(
                f"활성 보관함의 vault_name이 중복되었습니다: {vault.vault_name!r} "
                f"(항목 {names[name_key]} 및 {index})"
            )
        names[name_key] = index
        if vault.vault_path in paths:
            raise ConfigError(
                f"활성 보관함의 정규화 경로가 중복되었습니다: {vault.vault_path} "
                f"(항목 {paths[vault.vault_path]} 및 {index})"
            )
        paths[vault.vault_path] = index

    state_raw = _mapping(root.get("state"), "state")
    database_path = Path(
        _nonempty_string(state_raw.get("database_path"), "state.database_path")
    ).expanduser()
    if not database_path.is_absolute():
        raise ConfigError("state.database_path는 절대 경로여야 합니다.")
    database_path = database_path.resolve(strict=False)
    for vault in enabled:
        if _is_within(database_path, vault.vault_path):
            raise ConfigError(
                f"상태 DB는 보관함 밖에 있어야 합니다: {database_path}가 "
                f"{vault.vault_path} 하위에 있습니다."
            )

    logging_raw = _mapping(root.get("logging", {}), "logging")
    level = _nonempty_string(logging_raw.get("level", "INFO"), "logging.level").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError(f"지원하지 않는 logging.level입니다: {level}")

    return AppConfig(
        mattermost=MattermostConfig(
            url=url,
            token=token,
            verify_ssl=verify_ssl,
            request_timeout_seconds=request_timeout_seconds,
            immediate_retry_attempts=immediate_retry_attempts,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            retry_jitter_ratio=retry_jitter_ratio,
        ),
        obsidian_notifications=vaults,
        state=StateConfig(database_path=database_path),
        logging=LoggingConfig(level=level),
    )


def _parse_vault(index: int, item: object, *, validate_paths: bool) -> VaultConfig:
    prefix = f"obsidian_notifications[{index}]"
    raw = _mapping(item, prefix)
    enabled = _boolean(raw.get("enabled", True), f"{prefix}.enabled")
    vault_path = Path(
        _nonempty_string(raw.get("vault_path"), f"{prefix}.vault_path")
    ).expanduser()
    if not vault_path.is_absolute():
        raise ConfigError(f"{prefix}.vault_path는 절대 경로여야 합니다.")
    vault_path = vault_path.resolve(strict=False)
    if (
        enabled
        and validate_paths
        and (not vault_path.exists() or not vault_path.is_dir())
    ):
        raise ConfigError(
            f"활성 보관함 경로가 존재하는 디렉터리가 아닙니다: {vault_path}"
        )

    vault_name = _nonempty_string(raw.get("vault_name"), f"{prefix}.vault_name")
    channel_id = _optional_string(raw.get("channel_id"), f"{prefix}.channel_id")
    team_name = _optional_string(raw.get("team_name"), f"{prefix}.team_name")
    channel_name = _optional_string(raw.get("channel_name"), f"{prefix}.channel_name")
    if channel_id is None and (team_name is None or channel_name is None):
        raise ConfigError(
            f"{prefix}에는 channel_id 또는 team_name과 channel_name을 모두 지정해야 합니다."
        )

    ignores_raw = raw.get("ignore_folders", [".obsidian", ".trash"])
    if not isinstance(ignores_raw, list):
        raise ConfigError(f"{prefix}.ignore_folders는 문자열 배열이어야 합니다.")
    ignores: list[str] = []
    for ignore_index, value in enumerate(ignores_raw):
        ignore = _nonempty_string(value, f"{prefix}.ignore_folders[{ignore_index}]")
        normalized = PurePosixPath(ignore.replace("\\", "/"))
        if (
            normalized.is_absolute()
            or ".." in normalized.parts
            or normalized == PurePosixPath(".")
        ):
            raise ConfigError(
                f"무시 폴더는 보관함 기준 상대 경로여야 합니다: {ignore!r}"
            )
        text = normalized.as_posix().rstrip("/")
        if text not in ignores:
            ignores.append(text)

    settle_raw = raw.get("settle_seconds", 2)
    if isinstance(settle_raw, bool) or not isinstance(settle_raw, (int, float)):
        raise ConfigError(f"{prefix}.settle_seconds는 0 이상의 숫자여야 합니다.")
    settle_seconds = float(settle_raw)
    if not math.isfinite(settle_seconds) or settle_seconds < 0:
        raise ConfigError(f"{prefix}.settle_seconds는 0 이상이어야 합니다.")

    quiet_raw = raw.get("notification_quiet_seconds", 30)
    if isinstance(quiet_raw, bool) or not isinstance(quiet_raw, (int, float)):
        raise ConfigError(
            f"{prefix}.notification_quiet_seconds는 0 이상의 숫자여야 합니다."
        )
    notification_quiet_seconds = float(quiet_raw)
    if not math.isfinite(notification_quiet_seconds) or notification_quiet_seconds < 0:
        raise ConfigError(f"{prefix}.notification_quiet_seconds는 0 이상이어야 합니다.")

    patterns_raw = raw.get(
        "draft_name_patterns", [r"무제(?: \d+)?", r"Untitled(?: \d+)?"]
    )
    if not isinstance(patterns_raw, list):
        raise ConfigError(
            f"{prefix}.draft_name_patterns는 정규식 문자열 배열이어야 합니다."
        )
    draft_name_patterns: list[str] = []
    for pattern_index, value in enumerate(patterns_raw):
        pattern = _nonempty_string(
            value, f"{prefix}.draft_name_patterns[{pattern_index}]"
        )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(
                f"{prefix}.draft_name_patterns[{pattern_index}] 정규식이 "
                f"올바르지 않습니다: {exc}"
            ) from exc
        if pattern not in draft_name_patterns:
            draft_name_patterns.append(pattern)

    return VaultConfig(
        enabled=enabled,
        vault_path=vault_path,
        vault_name=vault_name,
        team_name=team_name,
        channel_name=channel_name,
        channel_id=channel_id,
        ignore_folders=tuple(ignores),
        settle_seconds=settle_seconds,
        notification_quiet_seconds=notification_quiet_seconds,
        draft_name_patterns=tuple(draft_name_patterns),
    )


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{field}는 YAML 객체여야 합니다.")
    return value


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field}는 비어 있지 않은 문자열이어야 합니다.")
    return value.strip()


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, field)


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{field}는 true 또는 false여야 합니다.")
    return value


def _positive_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field}는 0보다 큰 숫자여야 합니다.")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ConfigError(f"{field}는 0보다 큰 숫자여야 합니다.")
    return result


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{field}는 0 이상의 정수여야 합니다.")
    return value


def _number_in_range(
    value: object, field: str, *, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field}는 {minimum} 이상 {maximum} 이하의 숫자여야 합니다.")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ConfigError(f"{field}는 {minimum} 이상 {maximum} 이하이어야 합니다.")
    return result


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True
