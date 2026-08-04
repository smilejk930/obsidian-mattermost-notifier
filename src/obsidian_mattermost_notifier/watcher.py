from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
    FileSystemMovedEvent,
)
from watchdog.observers import Observer

from .config import VaultConfig
from .state import FileSnapshot

LOGGER = logging.getLogger(__name__)


def scan_markdown_files(vault: VaultConfig) -> list[FileSnapshot]:
    snapshots: list[FileSnapshot] = []
    for path in vault.vault_path.rglob("*"):
        relative_path = eligible_relative_path(vault, path)
        if relative_path is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            snapshots.append(
                FileSnapshot(relative_path, stat.st_size, stat.st_mtime_ns)
            )
    snapshots.sort(key=lambda snapshot: snapshot.relative_path)
    return snapshots


def eligible_relative_path(vault: VaultConfig, path: str | Path) -> str | None:
    candidate = Path(path).resolve(strict=False)
    try:
        relative = candidate.relative_to(vault.vault_path)
    except ValueError:
        return None
    if relative.suffix.lower() != ".md":
        return None
    relative_posix = PurePosixPath(*relative.parts)
    for ignored in vault.ignore_folders:
        ignored_path = PurePosixPath(ignored)
        if relative_posix == ignored_path or ignored_path in relative_posix.parents:
            return None
    return relative_posix.as_posix()


@dataclass(slots=True)
class _ScheduledFile:
    signature: tuple[int, int] | None
    due_at: float


class StableFileDispatcher:
    """Debounce file events until size and mtime stay unchanged for settle_seconds."""

    def __init__(
        self,
        settle_seconds: float,
        callback: Callable[[Path], None],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settle_seconds = settle_seconds
        self._callback = callback
        self._logger = logger or LOGGER
        self._condition = threading.Condition()
        self._scheduled: dict[Path, _ScheduledFile] = {}
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run, name="stable-file-dispatcher", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def submit(self, path: Path) -> None:
        signature = _file_signature(path)
        with self._condition:
            self._scheduled[path] = _ScheduledFile(
                signature=signature,
                due_at=time.monotonic() + self._settle_seconds,
            )
            self._condition.notify()

    def stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=max(15.0, self._settle_seconds + 1.0))

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._scheduled and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                path, scheduled = min(
                    self._scheduled.items(), key=lambda item: item[1].due_at
                )
                remaining = scheduled.due_at - time.monotonic()
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
                current = self._scheduled.get(path)
                if current is not scheduled:
                    continue
                self._scheduled.pop(path, None)

            signature = _file_signature(path)
            if signature is None:
                continue
            if signature != scheduled.signature:
                with self._condition:
                    self._scheduled[path] = _ScheduledFile(
                        signature=signature,
                        due_at=time.monotonic() + self._settle_seconds,
                    )
                    self._condition.notify()
                continue
            try:
                self._callback(path)
            except Exception:
                self._logger.exception("안정화된 파일 처리 실패: %s", path)


class VaultEventHandler(FileSystemEventHandler):
    def __init__(
        self,
        vault: VaultConfig,
        on_created: Callable[[Path], None],
        on_missing: Callable[[str], None],
    ) -> None:
        self._vault = vault
        self._on_created = on_created
        self._on_missing = on_missing

    def on_created(self, event: FileSystemEvent) -> None:
        if (
            not event.is_directory
            and eligible_relative_path(self._vault, event.src_path) is not None
        ):
            self._on_created(Path(event.src_path))

    def on_moved(self, event: FileSystemMovedEvent) -> None:
        if event.is_directory:
            return
        source_relative = eligible_relative_path(self._vault, event.src_path)
        if source_relative is not None:
            self._on_missing(source_relative)
        if eligible_relative_path(self._vault, event.dest_path) is not None:
            self._on_created(Path(event.dest_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        relative_path = eligible_relative_path(self._vault, event.src_path)
        if relative_path is not None:
            self._on_missing(relative_path)


class VaultWatcher:
    def __init__(
        self,
        vault: VaultConfig,
        on_stable_file: Callable[[Path], None],
        on_missing: Callable[[str], None],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.vault = vault
        self._logger = logger or LOGGER
        self._dispatcher = StableFileDispatcher(
            vault.settle_seconds, on_stable_file, logger=self._logger
        )
        self._handler = VaultEventHandler(vault, self._dispatcher.submit, on_missing)
        self._observer = Observer()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._dispatcher.start()
        try:
            self._observer.schedule(
                self._handler, str(self.vault.vault_path), recursive=True
            )
            self._observer.start()
        except Exception:
            self._dispatcher.stop()
            raise
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._observer.stop()
        self._observer.join(timeout=10)
        self._dispatcher.stop()
        self._started = False

    @property
    def is_alive(self) -> bool:
        return self._started and self._observer.is_alive()


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return stat.st_size, stat.st_mtime_ns
