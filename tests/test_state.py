from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from obsidian_mattermost_notifier.state import FileSnapshot, StateStore

NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)


def test_first_reconcile_creates_baseline_without_pending_posts(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "notifier.db")
    try:
        pending = store.reconcile(
            "vault", [FileSnapshot("existing.md", 10, 100)], observed_at=NOW
        )
        record = store.get("vault", "existing.md")

        assert pending == []
        assert record is not None
        assert record.status == "baseline"
        assert record.present
    finally:
        store.close()


def test_restart_discovers_new_file_and_deduplicates_events(tmp_path: Path) -> None:
    database = tmp_path / "notifier.db"
    first = StateStore(database)
    first.reconcile("vault", [FileSnapshot("existing.md", 10, 100)], observed_at=NOW)
    first.close()

    second = StateStore(database)
    try:
        pending = second.reconcile(
            "vault",
            [
                FileSnapshot("existing.md", 11, 101),
                FileSnapshot("during-stop.md", 20, 200),
            ],
            observed_at=NOW + timedelta(hours=1),
        )
        assert [record.relative_path for record in pending] == ["during-stop.md"]

        duplicate, should_notify = second.observe_new(
            "vault",
            FileSnapshot("during-stop.md", 21, 201),
            observed_at=NOW + timedelta(hours=2),
        )
        assert not should_notify
        assert duplicate.generation == 1

        second.mark_sent("vault", "during-stop.md", "post-1", generation=1)
        assert second.pending("vault") == []
        assert second.get("vault", "during-stop.md").post_id == "post-1"  # type: ignore[union-attr]
    finally:
        second.close()


def test_delete_then_same_path_recreation_is_a_new_generation(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "notifier.db")
    try:
        store.reconcile("vault", [FileSnapshot("again.md", 10, 100)], observed_at=NOW)
        store.mark_missing("vault", "again.md")

        recreated, should_notify = store.observe_new(
            "vault",
            FileSnapshot("again.md", 30, 300),
            observed_at=NOW + timedelta(days=1),
        )

        assert should_notify
        assert recreated.status == "pending"
        assert recreated.generation == 2
        assert recreated.first_observed_at == NOW + timedelta(days=1)
        with pytest.raises(KeyError):
            store.mark_sent("vault", "again.md", "stale-post", generation=1)
        assert store.pending("vault")[0].generation == 2
    finally:
        store.close()


def test_move_preserves_sent_document_identity_without_new_pending(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "notifier.db")
    try:
        store.reconcile("vault", [], observed_at=NOW)
        store.observe_new("vault", FileSnapshot("before.md", 10, 100), observed_at=NOW)
        store.mark_sent("vault", "before.md", "post-1", generation=1)

        moved = store.move_path("vault", "before.md", FileSnapshot("after.md", 20, 200))

        assert moved is not None
        assert moved.relative_path == "after.md"
        assert moved.status == "sent"
        assert moved.post_id == "post-1"
        assert store.get("vault", "before.md") is None
        assert store.pending("vault") == []
    finally:
        store.close()


def test_reconcile_quiet_deadline_survives_resume_pending(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "notifier.db")
    try:
        store.reconcile("vault", [], observed_at=NOW)
        ready_at = NOW + timedelta(seconds=30)
        store.reconcile(
            "vault",
            [FileSnapshot("while-stopped.md", 10, 100)],
            observed_at=NOW,
            notify_after=ready_at,
        )

        store.resume_pending(resumed_at=NOW + timedelta(seconds=1))

        assert store.due_pending(["vault"], now=ready_at - timedelta(seconds=1)) == []
        assert store.due_pending(["vault"], now=ready_at)[0].relative_path == (
            "while-stopped.md"
        )
    finally:
        store.close()


def test_failed_delivery_remains_pending(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "notifier.db")
    try:
        store.reconcile("vault", [], observed_at=NOW)
        record, should_notify = store.observe_new(
            "vault", FileSnapshot("new.md", 1, 1), observed_at=NOW
        )
        assert should_notify
        assert record.status == "pending"
        assert [item.relative_path for item in store.pending("vault")] == ["new.md"]
    finally:
        store.close()


def test_retry_schedule_and_nonretryable_block_are_persisted(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "notifier.db")
    try:
        store.reconcile("vault", [], observed_at=NOW)
        store.observe_new("vault", FileSnapshot("new.md", 1, 1), observed_at=NOW)
        assert [
            item.relative_path for item in store.due_pending(["vault"], now=NOW)
        ] == ["new.md"]

        retry_at = NOW + timedelta(seconds=30)
        store.mark_delivery_failure(
            "vault",
            "new.md",
            generation=1,
            error_code="http_503",
            next_retry_at=retry_at,
        )
        assert store.due_pending(["vault"], now=NOW + timedelta(seconds=29)) == []
        assert store.due_pending(["vault"], now=retry_at)[0].attempt_count == 1

        store.mark_delivery_failure(
            "vault",
            "new.md",
            generation=1,
            error_code="http_403",
            next_retry_at=None,
        )
        record = store.get("vault", "new.md")
        assert record is not None
        assert record.attempt_count == 2
        assert record.next_retry_at is None
        assert record.last_error == "http_403"
        assert store.due_pending(["vault"], now=NOW + timedelta(days=1)) == []

        store.resume_pending(resumed_at=NOW + timedelta(days=1))
        assert len(store.due_pending(["vault"], now=NOW + timedelta(days=1))) == 1
    finally:
        store.close()


def test_phase1_database_is_migrated_without_losing_pending_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "phase1.db"
    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            "CREATE TABLE vaults (vault_name TEXT PRIMARY KEY, initialized_at TEXT NOT NULL)"
        )
        connection.execute(
            """
            CREATE TABLE documents (
                vault_name TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 1,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                first_observed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                post_id TEXT,
                notified_at TEXT,
                present INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (vault_name, relative_path)
            )
            """
        )
        connection.execute(
            "INSERT INTO vaults VALUES (?, ?)", ("vault", NOW.isoformat())
        )
        connection.execute(
            """
            INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "vault",
                "pending.md",
                1,
                10,
                100,
                NOW.isoformat(),
                "pending",
                None,
                None,
                1,
            ),
        )
    connection.close()

    store = StateStore(database)
    try:
        record = store.get("vault", "pending.md")
        assert record is not None
        assert record.status == "pending"
        assert record.attempt_count == 0
        assert record.next_retry_at == NOW
        assert record.last_error is None
    finally:
        store.close()
