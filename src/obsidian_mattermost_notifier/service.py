from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .config import AppConfig, VaultConfig
from .mattermost import MattermostClient
from .message import build_message, document_title
from .state import DocumentRecord, FileSnapshot, StateStore
from .watcher import VaultWatcher, eligible_relative_path, scan_markdown_files

LOGGER = logging.getLogger(__name__)


class Publisher(Protocol):
    def channel_id_for(self, vault: VaultConfig) -> str: ...

    def post_message(self, channel_id: str, message: str) -> str: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class _VaultRuntime:
    config: VaultConfig
    channel_id: str
    watcher: VaultWatcher


class NotifierService:
    def __init__(
        self,
        config: AppConfig,
        *,
        state: StateStore | None = None,
        publisher: Publisher | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self._state = state or StateStore(config.state.database_path)
        self._publisher = publisher or MattermostClient(config.mattermost)
        self._logger = logger or LOGGER
        self._runtimes: dict[str, _VaultRuntime] = {}
        self._failed_vaults: dict[str, VaultConfig] = {}
        self._delivery_locks: dict[str, threading.Lock] = {}
        self._started = False
        self._closed = False

    def start(self) -> None:
        if self._started:
            return
        failures: list[str] = []
        for vault in self.config.enabled_vaults:
            try:
                self._start_vault(vault)
            except Exception as exc:  # noqa: BLE001 - isolate each configured vault
                failures.append(vault.vault_name)
                self._failed_vaults[vault.vault_name] = vault
                self._logger.error("보관함 시작 실패 [%s]: %s", vault.vault_name, exc)
        if not self._runtimes:
            joined = ", ".join(failures) or "없음"
            raise RuntimeError(f"시작할 수 있는 보관함이 없습니다. 실패: {joined}")
        self._started = True

    def stop(self) -> None:
        if self._closed:
            return
        for runtime in list(self._runtimes.values()):
            try:
                runtime.watcher.stop()
            except Exception as exc:  # noqa: BLE001 - best-effort shutdown
                self._logger.warning(
                    "보관함 감시 종료 실패 [%s]: %s", runtime.config.vault_name, exc
                )
        self._runtimes.clear()
        self._failed_vaults.clear()
        self._publisher.close()
        self._state.close()
        self._closed = True
        self._started = False

    def restart_failed_watchers(self) -> None:
        """Recreate dead observers without disturbing healthy vaults."""
        for vault_name, vault in list(self._failed_vaults.items()):
            try:
                self._start_vault(vault)
            except Exception as exc:  # noqa: BLE001 - retry boundary for one vault
                self._logger.error("보관함 시작 재시도 실패 [%s]: %s", vault_name, exc)
            else:
                self._failed_vaults.pop(vault_name, None)

        for vault_name, runtime in list(self._runtimes.items()):
            if runtime.watcher.is_alive:
                continue
            self._logger.warning(
                "보관함 감시기가 중단되어 재시작합니다: %s", vault_name
            )
            try:
                runtime.watcher.stop()
            except Exception as cleanup_exc:  # noqa: BLE001 - replace a dead watcher
                self._logger.debug(
                    "중단된 감시기 정리 실패 [%s]: %s", vault_name, cleanup_exc
                )
            watcher: VaultWatcher | None = None
            try:
                watcher = self._new_watcher(runtime.config, runtime.channel_id)
                watcher.start()
                pending = self._state.reconcile(
                    vault_name,
                    scan_markdown_files(runtime.config),
                    observed_at=datetime.now(UTC),
                )
                self._runtimes[vault_name] = _VaultRuntime(
                    config=runtime.config,
                    channel_id=runtime.channel_id,
                    watcher=watcher,
                )
                for record in pending:
                    self._deliver(runtime.config, runtime.channel_id, record)
            except Exception as exc:  # noqa: BLE001 - isolate watcher restarts
                if watcher is not None:
                    try:
                        watcher.stop()
                    except Exception as cleanup_exc:  # noqa: BLE001 - best-effort cleanup
                        self._logger.debug(
                            "실패한 감시기 정리 실패 [%s]: %s", vault_name, cleanup_exc
                        )
                self._logger.error("보관함 감시 재시작 실패 [%s]: %s", vault_name, exc)

    def _start_vault(self, vault: VaultConfig) -> None:
        channel_id = self._publisher.channel_id_for(vault)
        watcher = self._new_watcher(vault, channel_id)
        initialized = self._state.is_initialized(vault.vault_name)

        if initialized:
            watcher.start()
            try:
                pending = self._state.reconcile(
                    vault.vault_name,
                    scan_markdown_files(vault),
                    observed_at=datetime.now(UTC),
                )
            except Exception:
                watcher.stop()
                raise
        else:
            pending = self._state.reconcile(
                vault.vault_name,
                scan_markdown_files(vault),
                observed_at=datetime.now(UTC),
            )
            watcher.start()

        self._runtimes[vault.vault_name] = _VaultRuntime(vault, channel_id, watcher)
        self._failed_vaults.pop(vault.vault_name, None)
        self._logger.info(
            "보관함 감시 시작: %s (%s)", vault.vault_name, vault.vault_path
        )
        for record in pending:
            self._deliver(vault, channel_id, record)

    def _new_watcher(self, vault: VaultConfig, channel_id: str) -> VaultWatcher:
        return VaultWatcher(
            vault,
            lambda path: self._on_stable_file(vault, channel_id, path),
            lambda relative: self._state.mark_missing(vault.vault_name, relative),
            logger=self._logger,
        )

    def _on_stable_file(self, vault: VaultConfig, channel_id: str, path: Path) -> None:
        relative_path = eligible_relative_path(vault, path)
        if relative_path is None:
            return
        try:
            stat = path.stat()
        except OSError:
            return
        record, _ = self._state.observe_new(
            vault.vault_name,
            FileSnapshot(relative_path, stat.st_size, stat.st_mtime_ns),
            observed_at=datetime.now(UTC),
        )
        if record.status == "pending" and record.present:
            self._deliver(vault, channel_id, record)

    def _deliver(
        self, vault: VaultConfig, channel_id: str, candidate: DocumentRecord
    ) -> None:
        lock = self._delivery_locks.setdefault(vault.vault_name, threading.Lock())
        with lock:
            current = self._state.get(vault.vault_name, candidate.relative_path)
            if (
                current is None
                or current.status != "pending"
                or not current.present
                or current.generation != candidate.generation
            ):
                return
            path = vault.vault_path / Path(current.relative_path)
            message = build_message(
                vault_name=vault.vault_name,
                relative_path=current.relative_path,
                title=document_title(path),
                observed_at=current.first_observed_at,
            )
            try:
                post_id = self._publisher.post_message(channel_id, message)
            except Exception as exc:  # noqa: BLE001 - publisher implementations vary
                # Do not log the document body or credentials. The pending row is retained.
                self._logger.error(
                    "Mattermost 게시 실패 [%s/%s]: %s",
                    vault.vault_name,
                    current.relative_path,
                    exc,
                )
                return
            self._state.mark_sent(
                vault.vault_name,
                current.relative_path,
                post_id,
                generation=current.generation,
            )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
