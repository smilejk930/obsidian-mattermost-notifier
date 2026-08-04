from pathlib import Path

import yaml


def test_bridge_healthcheck_uses_restart_verdict_not_sync_readiness() -> None:
    compose = yaml.safe_load(Path("compose.example.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["livesync-bridge"]
    command = service["healthcheck"]["test"]

    assert command[:3] == ["CMD", "deno", "eval"]
    assert "-A" not in command
    assert "h.restartWorthy === true" in command[-1]
    assert "!h.ok" not in command[-1]
    assert "init" not in service
