from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from obsidian_mattermost_notifier.config import VaultConfig
from obsidian_mattermost_notifier.watcher import (
    StableFileDispatcher,
    VaultEventHandler,
    VaultWatcher,
    eligible_relative_path,
    scan_markdown_files,
)


def vault_config(path: Path, *, settle_seconds: float = 0.02) -> VaultConfig:
    return VaultConfig(
        enabled=True,
        vault_path=path.resolve(),
        vault_name="vault",
        team_name="team",
        channel_name="channel",
        channel_id=None,
        ignore_folders=(".obsidian", ".trash", "private/archive"),
        settle_seconds=settle_seconds,
        notification_quiet_seconds=settle_seconds,
    )


def test_scan_only_includes_markdown_outside_ignored_folders(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "one.md").write_text("# One", encoding="utf-8")
    (tmp_path / "docs" / "other.txt").write_text("no", encoding="utf-8")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "hidden.md").write_text("hidden", encoding="utf-8")
    (tmp_path / "private" / "archive").mkdir(parents=True)
    (tmp_path / "private" / "archive" / "old.md").write_text("old", encoding="utf-8")

    snapshots = scan_markdown_files(vault_config(tmp_path))

    assert [snapshot.relative_path for snapshot in snapshots] == ["docs/one.md"]
    assert (
        eligible_relative_path(vault_config(tmp_path), tmp_path.parent / "outside.md")
        is None
    )


def test_handler_accepts_create_move_and_modify_activity() -> None:
    root = Path("/tmp/test-vault").resolve()
    vault = vault_config(root)
    created: list[Path] = []
    missing: list[str] = []
    moved: list[tuple[str, Path]] = []
    handler = VaultEventHandler(
        vault, created.append, lambda *args: moved.append(args), missing.append
    )

    handler.dispatch(FileCreatedEvent(str(root / "created.md")))
    handler.dispatch(FileCreatedEvent(str(root / ".obsidian" / "ignored.md")))
    handler.dispatch(FileModifiedEvent(str(root / "created.md")))
    handler.dispatch(FileMovedEvent(str(root / "old.md"), str(root / "renamed.md")))
    handler.dispatch(FileDeletedEvent(str(root / "renamed.md")))

    assert created == [root / "created.md", root / "created.md", root / "renamed.md"]
    assert moved == [("old.md", root / "renamed.md")]
    assert missing == ["renamed.md"]


def test_dispatcher_waits_until_size_and_mtime_are_stable(tmp_path: Path) -> None:
    document = tmp_path / "new.md"
    document.write_text("first", encoding="utf-8")
    delivered = threading.Event()
    dispatcher = StableFileDispatcher(0.04, lambda _path: delivered.set())
    dispatcher.start()
    try:
        dispatcher.submit(document)
        time.sleep(0.02)
        document.write_text("second write", encoding="utf-8")

        assert delivered.wait(timeout=1)
    finally:
        dispatcher.stop()


def test_vault_watcher_observes_real_file_creation(tmp_path: Path) -> None:
    delivered = threading.Event()
    watcher = VaultWatcher(
        vault_config(tmp_path),
        lambda _path: delivered.set(),
        lambda _source, _destination: None,
        lambda _relative: None,
    )
    watcher.start()
    try:
        (tmp_path / "created.md").write_text("# Created", encoding="utf-8")
        assert delivered.wait(timeout=2)
    finally:
        watcher.stop()


def test_vault_watcher_uses_modification_as_quiet_period_activity(
    tmp_path: Path,
) -> None:
    document = tmp_path / "existing.md"
    document.write_text("initial", encoding="utf-8")
    delivered = threading.Event()
    watcher = VaultWatcher(
        vault_config(tmp_path),
        lambda _path: delivered.set(),
        lambda _source, _destination: None,
        lambda _relative: None,
    )
    watcher.start()
    try:
        document.write_text("modified", encoding="utf-8")
        assert delivered.wait(timeout=1)
    finally:
        watcher.stop()
