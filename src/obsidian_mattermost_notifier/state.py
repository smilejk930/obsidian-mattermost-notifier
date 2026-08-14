from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    relative_path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    vault_name: str
    relative_path: str
    generation: int
    size: int
    mtime_ns: int
    first_observed_at: datetime
    status: str
    post_id: str | None
    notified_at: datetime | None
    present: bool
    attempt_count: int
    next_retry_at: datetime | None
    last_error: str | None


class StateStore:
    """Thread-safe SQLite state for baselines, pending posts, and deduplication."""

    def __init__(self, database_path: str | Path) -> None:
        path = Path(database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS vaults (
                    vault_name TEXT PRIMARY KEY,
                    initialized_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    vault_name TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 1,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    first_observed_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('baseline', 'pending', 'sent')),
                    post_id TEXT,
                    notified_at TEXT,
                    present INTEGER NOT NULL DEFAULT 1 CHECK (present IN (0, 1)),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    last_error TEXT,
                    PRIMARY KEY (vault_name, relative_path),
                    FOREIGN KEY (vault_name) REFERENCES vaults(vault_name) ON DELETE CASCADE
                )
                """
            )
            self._migrate_schema()

    def is_initialized(self, vault_name: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM vaults WHERE vault_name = ?", (vault_name,)
            ).fetchone()
        return row is not None

    def reconcile(
        self,
        vault_name: str,
        files: Iterable[FileSnapshot],
        *,
        observed_at: datetime | None = None,
        notify_after: datetime | None = None,
    ) -> list[DocumentRecord]:
        """Baseline a new vault or return pending documents for an initialized vault."""
        now = _as_utc(observed_at or datetime.now(UTC))
        pending_at = _as_utc(notify_after) if notify_after is not None else now
        snapshots = {
            normalize_relative_path(snapshot.relative_path): FileSnapshot(
                normalize_relative_path(snapshot.relative_path),
                snapshot.size,
                snapshot.mtime_ns,
            )
            for snapshot in files
        }
        with self._lock, self._connection:
            initialized = self._connection.execute(
                "SELECT 1 FROM vaults WHERE vault_name = ?", (vault_name,)
            ).fetchone()
            if initialized is None:
                self._connection.execute(
                    "INSERT INTO vaults(vault_name, initialized_at) VALUES (?, ?)",
                    (vault_name, _serialize_time(now)),
                )
                self._connection.executemany(
                    """
                    INSERT INTO documents(
                        vault_name, relative_path, size, mtime_ns,
                        first_observed_at, status, present
                    ) VALUES (?, ?, ?, ?, ?, 'baseline', 1)
                    """,
                    (
                        (
                            vault_name,
                            snapshot.relative_path,
                            snapshot.size,
                            snapshot.mtime_ns,
                            _serialize_time(now),
                        )
                        for snapshot in snapshots.values()
                    ),
                )
                return []

            existing_rows = {
                row["relative_path"]: row
                for row in self._connection.execute(
                    "SELECT * FROM documents WHERE vault_name = ?", (vault_name,)
                )
            }
            if snapshots:
                placeholders = ",".join("?" for _ in snapshots)
                self._connection.execute(
                    f"UPDATE documents SET present = 0 WHERE vault_name = ? "
                    f"AND relative_path NOT IN ({placeholders})",
                    (vault_name, *snapshots.keys()),
                )
            else:
                self._connection.execute(
                    "UPDATE documents SET present = 0 WHERE vault_name = ?",
                    (vault_name,),
                )

            for relative_path, snapshot in snapshots.items():
                row = existing_rows.get(relative_path)
                if row is None:
                    self._insert_pending(
                        vault_name,
                        snapshot,
                        now,
                        generation=1,
                        notify_at=pending_at,
                    )
                elif not bool(row["present"]):
                    self._connection.execute(
                        """
                        UPDATE documents
                        SET generation = ?, size = ?, mtime_ns = ?, first_observed_at = ?,
                            status = 'pending', post_id = NULL, notified_at = NULL, present = 1,
                            attempt_count = 0, next_retry_at = ?, last_error = NULL
                        WHERE vault_name = ? AND relative_path = ?
                        """,
                        (
                            int(row["generation"]) + 1,
                            snapshot.size,
                            snapshot.mtime_ns,
                            _serialize_time(now),
                            _serialize_time(pending_at),
                            vault_name,
                            relative_path,
                        ),
                    )
                else:
                    self._connection.execute(
                        """
                        UPDATE documents SET size = ?, mtime_ns = ?, present = 1
                        WHERE vault_name = ? AND relative_path = ?
                        """,
                        (snapshot.size, snapshot.mtime_ns, vault_name, relative_path),
                    )
            return self._pending_locked(vault_name)

    def observe_new(
        self,
        vault_name: str,
        snapshot: FileSnapshot,
        *,
        observed_at: datetime | None = None,
    ) -> tuple[DocumentRecord, bool]:
        now = _as_utc(observed_at or datetime.now(UTC))
        snapshot = FileSnapshot(
            normalize_relative_path(snapshot.relative_path),
            snapshot.size,
            snapshot.mtime_ns,
        )
        with self._lock, self._connection:
            self._ensure_vault(vault_name, now)
            row = self._connection.execute(
                "SELECT * FROM documents WHERE vault_name = ? AND relative_path = ?",
                (vault_name, snapshot.relative_path),
            ).fetchone()
            should_notify = False
            if row is None:
                self._insert_pending(vault_name, snapshot, now, generation=1)
                should_notify = True
            elif not bool(row["present"]):
                self._connection.execute(
                    """
                    UPDATE documents
                    SET generation = ?, size = ?, mtime_ns = ?, first_observed_at = ?,
                        status = 'pending', post_id = NULL, notified_at = NULL, present = 1,
                        attempt_count = 0, next_retry_at = ?, last_error = NULL
                    WHERE vault_name = ? AND relative_path = ?
                    """,
                    (
                        int(row["generation"]) + 1,
                        snapshot.size,
                        snapshot.mtime_ns,
                        _serialize_time(now),
                        _serialize_time(now),
                        vault_name,
                        snapshot.relative_path,
                    ),
                )
                should_notify = True
            else:
                self._connection.execute(
                    """
                    UPDATE documents SET size = ?, mtime_ns = ?
                    WHERE vault_name = ? AND relative_path = ?
                    """,
                    (
                        snapshot.size,
                        snapshot.mtime_ns,
                        vault_name,
                        snapshot.relative_path,
                    ),
                )
            record = self._get_locked(vault_name, snapshot.relative_path)
            return record, should_notify

    def mark_missing(self, vault_name: str, relative_path: str) -> None:
        normalized = normalize_relative_path(relative_path)
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE documents SET present = 0 WHERE vault_name = ? AND relative_path = ?",
                (vault_name, normalized),
            )

    def move_path(
        self,
        vault_name: str,
        source_relative_path: str,
        destination: FileSnapshot,
    ) -> DocumentRecord | None:
        source = normalize_relative_path(source_relative_path)
        destination = FileSnapshot(
            normalize_relative_path(destination.relative_path),
            destination.size,
            destination.mtime_ns,
        )
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT 1 FROM documents WHERE vault_name = ? AND relative_path = ?",
                (vault_name, source),
            ).fetchone()
            if row is None:
                return None
            if source != destination.relative_path:
                self._connection.execute(
                    "DELETE FROM documents WHERE vault_name = ? AND relative_path = ?",
                    (vault_name, destination.relative_path),
                )
            self._connection.execute(
                """
                UPDATE documents
                SET relative_path = ?, size = ?, mtime_ns = ?, present = 1
                WHERE vault_name = ? AND relative_path = ?
                """,
                (
                    destination.relative_path,
                    destination.size,
                    destination.mtime_ns,
                    vault_name,
                    source,
                ),
            )
            return self._get_locked(vault_name, destination.relative_path)

    def mark_sent(
        self,
        vault_name: str,
        relative_path: str,
        post_id: str,
        *,
        generation: int,
        notified_at: datetime | None = None,
    ) -> None:
        normalized = normalize_relative_path(relative_path)
        now = _as_utc(notified_at or datetime.now(UTC))
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE documents
                SET status = 'sent', post_id = ?, notified_at = ?,
                    next_retry_at = NULL, last_error = NULL
                WHERE vault_name = ? AND relative_path = ?
                    AND generation = ? AND status = 'pending'
                """,
                (
                    post_id,
                    _serialize_time(now),
                    vault_name,
                    normalized,
                    generation,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(
                    f"pending 문서를 찾을 수 없습니다: {vault_name}/{normalized}"
                )

    def mark_delivery_failure(
        self,
        vault_name: str,
        relative_path: str,
        *,
        generation: int,
        error_code: str,
        next_retry_at: datetime | None,
    ) -> None:
        normalized = normalize_relative_path(relative_path)
        serialized_retry = (
            _serialize_time(next_retry_at) if next_retry_at is not None else None
        )
        safe_error = error_code[:200]
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE documents
                SET attempt_count = attempt_count + 1,
                    next_retry_at = ?, last_error = ?
                WHERE vault_name = ? AND relative_path = ?
                    AND generation = ? AND status = 'pending' AND present = 1
                """,
                (
                    serialized_retry,
                    safe_error,
                    vault_name,
                    normalized,
                    generation,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(
                    f"pending 문서를 찾을 수 없습니다: {vault_name}/{normalized}"
                )

    def resume_pending(self, *, resumed_at: datetime | None = None) -> None:
        now = _as_utc(resumed_at or datetime.now(UTC))
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE documents SET next_retry_at = ?
                WHERE status = 'pending' AND present = 1
                    AND (attempt_count > 0 OR next_retry_at IS NULL)
                """,
                (_serialize_time(now),),
            )

    def due_pending(
        self,
        vault_names: Iterable[str],
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[DocumentRecord]:
        names = tuple(dict.fromkeys(vault_names))
        if not names:
            return []
        current = _as_utc(now or datetime.now(UTC))
        placeholders = ",".join("?" for _ in names)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM documents
                WHERE vault_name IN ({placeholders})
                    AND status = 'pending' AND present = 1
                    AND next_retry_at IS NOT NULL AND next_retry_at <= ?
                ORDER BY next_retry_at, first_observed_at, relative_path
                LIMIT ?
                """,
                (*names, _serialize_time(current), limit),
            ).fetchall()
        return [_record(row) for row in rows]

    def next_retry_time(self, vault_names: Iterable[str]) -> datetime | None:
        names = tuple(dict.fromkeys(vault_names))
        if not names:
            return None
        placeholders = ",".join("?" for _ in names)
        with self._lock:
            row = self._connection.execute(
                f"""
                SELECT MIN(next_retry_at) AS next_retry_at FROM documents
                WHERE vault_name IN ({placeholders})
                    AND status = 'pending' AND present = 1
                    AND next_retry_at IS NOT NULL
                """,
                names,
            ).fetchone()
        value = row["next_retry_at"] if row is not None else None
        return _parse_time(value) if value else None

    def pending(self, vault_name: str) -> list[DocumentRecord]:
        with self._lock:
            return self._pending_locked(vault_name)

    def get(self, vault_name: str, relative_path: str) -> DocumentRecord | None:
        normalized = normalize_relative_path(relative_path)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM documents WHERE vault_name = ? AND relative_path = ?",
                (vault_name, normalized),
            ).fetchone()
            return _record(row) if row is not None else None

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _ensure_vault(self, vault_name: str, now: datetime) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO vaults(vault_name, initialized_at) VALUES (?, ?)",
            (vault_name, _serialize_time(now)),
        )

    def _migrate_schema(self) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(documents)")
        }
        migrations = {
            "attempt_count": (
                "ALTER TABLE documents ADD COLUMN "
                "attempt_count INTEGER NOT NULL DEFAULT 0"
            ),
            "next_retry_at": "ALTER TABLE documents ADD COLUMN next_retry_at TEXT",
            "last_error": "ALTER TABLE documents ADD COLUMN last_error TEXT",
        }
        for column, statement in migrations.items():
            if column not in columns:
                self._connection.execute(statement)
        self._connection.execute(
            """
            UPDATE documents SET next_retry_at = first_observed_at
            WHERE status = 'pending' AND next_retry_at IS NULL
            """
        )
        self._connection.execute("PRAGMA user_version = 2")

    def _insert_pending(
        self,
        vault_name: str,
        snapshot: FileSnapshot,
        now: datetime,
        *,
        generation: int,
        notify_at: datetime | None = None,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO documents(
                vault_name, relative_path, generation, size, mtime_ns,
                first_observed_at, status, present, next_retry_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, ?)
            """,
            (
                vault_name,
                snapshot.relative_path,
                generation,
                snapshot.size,
                snapshot.mtime_ns,
                _serialize_time(now),
                _serialize_time(notify_at or now),
            ),
        )

    def _pending_locked(self, vault_name: str) -> list[DocumentRecord]:
        rows = self._connection.execute(
            """
            SELECT * FROM documents
            WHERE vault_name = ? AND status = 'pending' AND present = 1
            ORDER BY first_observed_at, relative_path
            """,
            (vault_name,),
        ).fetchall()
        return [_record(row) for row in rows]

    def _get_locked(self, vault_name: str, relative_path: str) -> DocumentRecord:
        row = self._connection.execute(
            "SELECT * FROM documents WHERE vault_name = ? AND relative_path = ?",
            (vault_name, relative_path),
        ).fetchone()
        assert row is not None
        return _record(row)


def normalize_relative_path(path: str) -> str:
    candidate = PurePosixPath(path.replace("\\", "/"))
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError(f"보관함 기준 상대 경로가 아닙니다: {path!r}")
    normalized = candidate.as_posix()
    if normalized in {"", "."}:
        raise ValueError(f"보관함 기준 상대 경로가 아닙니다: {path!r}")
    return normalized


def _record(row: sqlite3.Row) -> DocumentRecord:
    return DocumentRecord(
        vault_name=row["vault_name"],
        relative_path=row["relative_path"],
        generation=int(row["generation"]),
        size=int(row["size"]),
        mtime_ns=int(row["mtime_ns"]),
        first_observed_at=_parse_time(row["first_observed_at"]),
        status=row["status"],
        post_id=row["post_id"],
        notified_at=_parse_time(row["notified_at"]) if row["notified_at"] else None,
        present=bool(row["present"]),
        attempt_count=int(row["attempt_count"]),
        next_retry_at=(
            _parse_time(row["next_retry_at"]) if row["next_retry_at"] else None
        ),
        last_error=row["last_error"],
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("시각에는 timezone 정보가 필요합니다.")
    return value.astimezone(UTC)


def _serialize_time(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
