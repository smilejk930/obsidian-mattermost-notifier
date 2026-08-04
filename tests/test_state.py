from __future__ import annotations

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
