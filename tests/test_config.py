from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from obsidian_mattermost_notifier.config import ConfigError, parse_config


def config_data(tmp_path: Path) -> dict:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return {
        "mattermost": {
            "url": "https://mattermost.example.com/",
            "token": "test-token",
            "verify_ssl": True,
        },
        "obsidian_notifications": [
            {
                "enabled": True,
                "vault_path": str(vault),
                "vault_name": "main-vault",
                "team_name": "dev team",
                "channel_name": "docs",
                "ignore_folders": [".obsidian", "archive/private"],
                "settle_seconds": 1.5,
            }
        ],
        "state": {"database_path": str(tmp_path / "state" / "notifier.db")},
        "logging": {"level": "debug"},
    }


def test_parse_multiple_vaults_and_channel_id_priority(tmp_path: Path) -> None:
    raw = config_data(tmp_path)
    second = tmp_path / "second"
    second.mkdir()
    raw["obsidian_notifications"].append(
        {
            "enabled": True,
            "vault_path": str(second),
            "vault_name": "second-vault",
            "channel_id": "channel-123",
            "settle_seconds": 0,
        }
    )

    config = parse_config(raw)

    assert config.mattermost.url == "https://mattermost.example.com"
    assert config.logging.level == "DEBUG"
    assert len(config.enabled_vaults) == 2
    assert config.enabled_vaults[1].channel_id == "channel-123"
    assert config.enabled_vaults[1].team_name is None


@pytest.mark.parametrize("field", ["vault_name", "vault_path"])
def test_duplicate_enabled_vault_identity_is_rejected(
    tmp_path: Path, field: str
) -> None:
    raw = config_data(tmp_path)
    duplicate = deepcopy(raw["obsidian_notifications"][0])
    if field == "vault_name":
        other = tmp_path / "other"
        other.mkdir()
        duplicate["vault_path"] = str(other)
        duplicate["vault_name"] = "MAIN-VAULT"
    else:
        duplicate["vault_name"] = "other-name"
        duplicate["vault_path"] = str(tmp_path / "vault" / ".")
    raw["obsidian_notifications"].append(duplicate)

    with pytest.raises(ConfigError, match="중복"):
        parse_config(raw)


def test_missing_enabled_path_is_reported_but_disabled_path_is_allowed(
    tmp_path: Path,
) -> None:
    raw = config_data(tmp_path)
    raw["obsidian_notifications"][0]["vault_path"] = str(tmp_path / "missing")

    with pytest.raises(ConfigError, match="존재하는 디렉터리"):
        parse_config(raw)

    raw["obsidian_notifications"][0]["enabled"] = False
    valid = tmp_path / "valid"
    valid.mkdir()
    raw["obsidian_notifications"].append(
        {
            "enabled": True,
            "vault_path": str(valid),
            "vault_name": "valid",
            "channel_id": "channel",
        }
    )
    config = parse_config(raw)
    assert [vault.vault_name for vault in config.enabled_vaults] == ["valid"]


def test_database_must_be_outside_enabled_vault(tmp_path: Path) -> None:
    raw = config_data(tmp_path)
    raw["state"]["database_path"] = str(tmp_path / "vault" / "notifier.db")

    with pytest.raises(ConfigError, match="보관함 밖"):
        parse_config(raw)


def test_change_me_token_and_string_boolean_are_rejected(tmp_path: Path) -> None:
    raw = config_data(tmp_path)
    raw["mattermost"]["token"] = "CHANGE_ME"
    with pytest.raises(ConfigError, match="CHANGE_ME"):
        parse_config(raw)

    raw = config_data(tmp_path)
    raw["mattermost"]["verify_ssl"] = "false"
    with pytest.raises(ConfigError, match="true 또는 false"):
        parse_config(raw)
