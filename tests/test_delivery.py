from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from obsidian_mattermost_notifier.config import VaultConfig
from obsidian_mattermost_notifier.delivery import (
    DeliveryTarget,
    DeliveryWorker,
)
from obsidian_mattermost_notifier.mattermost import MattermostRequestError
from obsidian_mattermost_notifier.retry import RetryPolicy
from obsidian_mattermost_notifier.state import FileSnapshot, StateStore


def vault(path: Path, name: str) -> VaultConfig:
    return VaultConfig(
        enabled=True,
        vault_path=path,
        vault_name=name,
        team_name=None,
        channel_name=None,
        channel_id=f"channel-{name}",
        ignore_folders=(),
        settle_seconds=0,
    )


def wait_until(predicate, *, timeout: float = 2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def test_retry_policy_uses_capped_backoff_jitter_and_retry_after() -> None:
    policy = RetryPolicy(
        immediate_retry_attempts=3,
        base_seconds=2,
        max_seconds=30,
        jitter_ratio=0.25,
    )

    assert policy.delay_seconds(1, random_value=0.5) == 2
    assert policy.delay_seconds(2, random_value=0.5) == 4
    assert policy.delay_seconds(4, random_value=0.5) == 30
    assert policy.delay_seconds(1, random_value=0) == 1.5
    assert policy.delay_seconds(1, retry_after=17, random_value=0.5) == 17


def test_worker_recovers_without_service_restart(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "notifier.db")
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    document = vault_path / "retry.md"
    document.write_text("# Retry", encoding="utf-8")
    now = datetime.now(UTC)
    store.reconcile("vault", [], observed_at=now)
    store.observe_new(
        "vault",
        FileSnapshot("retry.md", document.stat().st_size, document.stat().st_mtime_ns),
        observed_at=now,
    )

    class RecoveringPublisher:
        def __init__(self) -> None:
            self.attempts = 0

        def post_message(self, channel_id: str, message: str) -> str:
            self.attempts += 1
            if self.attempts == 1:
                raise MattermostRequestError(
                    "temporary",
                    code="http_503",
                    retryable=True,
                    status_code=503,
                )
            return "post-ok"

    publisher = RecoveringPublisher()
    configured_vault = vault(vault_path, "vault")
    worker = DeliveryWorker(
        store,
        publisher,
        lambda: {"vault": DeliveryTarget(configured_vault, "channel-vault")},
        RetryPolicy(2, 0.02, 0.05, 0),
    )
    worker.start()
    worker.wake()
    try:
        wait_until(lambda: store.get("vault", "retry.md").status == "sent")  # type: ignore[union-attr]
        record = store.get("vault", "retry.md")
        assert record is not None
        assert record.post_id == "post-ok"
        assert record.attempt_count == 1
        assert publisher.attempts == 2
    finally:
        worker.stop()
        store.close()


def test_nonretryable_failure_does_not_block_other_vault(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "notifier.db")
    targets: dict[str, DeliveryTarget] = {}
    now = datetime.now(UTC)
    for name in ("broken", "healthy"):
        vault_path = tmp_path / name
        vault_path.mkdir()
        document = vault_path / f"{name}.md"
        document.write_text(f"# {name}", encoding="utf-8")
        store.reconcile(name, [], observed_at=now)
        store.observe_new(
            name,
            FileSnapshot(
                document.name, document.stat().st_size, document.stat().st_mtime_ns
            ),
            observed_at=now,
        )
        targets[name] = DeliveryTarget(vault(vault_path, name), f"channel-{name}")

    class IsolatedPublisher:
        def __init__(self) -> None:
            self.channels: list[str] = []

        def post_message(self, channel_id: str, message: str) -> str:
            self.channels.append(channel_id)
            if channel_id == "channel-broken":
                raise MattermostRequestError(
                    "forbidden",
                    code="http_403",
                    retryable=False,
                    status_code=403,
                )
            return "healthy-post"

    publisher = IsolatedPublisher()
    worker = DeliveryWorker(
        store,
        publisher,
        lambda: targets,
        RetryPolicy(1, 0.01, 0.02, 0),
    )
    worker.start()
    worker.wake()
    try:
        wait_until(lambda: len(publisher.channels) == 2)
        broken = store.get("broken", "broken.md")
        healthy = store.get("healthy", "healthy.md")
        assert broken is not None and broken.status == "pending"
        assert broken.next_retry_at is None
        assert broken.last_error == "http_403"
        assert healthy is not None and healthy.status == "sent"
    finally:
        worker.stop()
        store.close()


def test_slow_vault_post_does_not_delay_healthy_vault(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "notifier.db")
    targets: dict[str, DeliveryTarget] = {}
    now = datetime.now(UTC)
    for name, filename in (("broken", "a.md"), ("healthy", "z.md")):
        vault_path = tmp_path / name
        vault_path.mkdir()
        document = vault_path / filename
        document.write_text(f"# {name}", encoding="utf-8")
        store.reconcile(name, [], observed_at=now)
        store.observe_new(
            name,
            FileSnapshot(
                filename, document.stat().st_size, document.stat().st_mtime_ns
            ),
            observed_at=now,
        )
        targets[name] = DeliveryTarget(vault(vault_path, name), f"channel-{name}")

    blocked = threading.Event()
    release = threading.Event()

    class SlowPublisher:
        def post_message(self, channel_id: str, message: str) -> str:
            if channel_id == "channel-broken":
                blocked.set()
                release.wait(timeout=2)
                raise MattermostRequestError(
                    "timeout",
                    code="timeout",
                    retryable=True,
                )
            return "healthy-post"

    worker = DeliveryWorker(
        store,
        SlowPublisher(),
        lambda: targets,
        RetryPolicy(1, 0.01, 0.02, 0),
        max_workers=2,
    )
    worker.start()
    worker.wake()
    try:
        assert blocked.wait(timeout=1)
        wait_until(
            lambda: store.get("healthy", "z.md").status == "sent",  # type: ignore[union-attr]
            timeout=0.5,
        )
    finally:
        release.set()
        worker.stop()
        store.close()


def test_late_response_cannot_complete_recreated_generation(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "notifier.db")
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    document = vault_path / "same.md"
    document.write_text("# First", encoding="utf-8")
    now = datetime.now(UTC)
    store.reconcile("vault", [], observed_at=now)
    store.observe_new(
        "vault",
        FileSnapshot("same.md", document.stat().st_size, document.stat().st_mtime_ns),
        observed_at=now,
    )
    first_started = threading.Event()
    release_first = threading.Event()

    class GenerationPublisher:
        def __init__(self) -> None:
            self.attempts = 0

        def post_message(self, channel_id: str, message: str) -> str:
            self.attempts += 1
            if self.attempts == 1:
                first_started.set()
                release_first.wait(timeout=2)
                return "old-post"
            return "new-post"

    publisher = GenerationPublisher()
    configured_vault = vault(vault_path, "vault")
    worker = DeliveryWorker(
        store,
        publisher,
        lambda: {"vault": DeliveryTarget(configured_vault, "channel-vault")},
        RetryPolicy(1, 0.01, 0.02, 0),
    )
    worker.start()
    worker.wake()
    try:
        assert first_started.wait(timeout=1)
        store.mark_missing("vault", "same.md")
        document.write_text("# Recreated", encoding="utf-8")
        store.observe_new(
            "vault",
            FileSnapshot(
                "same.md", document.stat().st_size, document.stat().st_mtime_ns
            ),
            observed_at=datetime.now(UTC),
        )
        release_first.set()

        wait_until(lambda: store.get("vault", "same.md").status == "sent")  # type: ignore[union-attr]
        record = store.get("vault", "same.md")
        assert record is not None
        assert record.generation == 2
        assert record.post_id == "new-post"
        assert publisher.attempts == 2
    finally:
        release_first.set()
        worker.stop()
        store.close()
