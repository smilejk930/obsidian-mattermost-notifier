from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from .config import AppConfig, VaultConfig
from .delivery import DeliveryTarget, DeliveryWorker
from .mattermost import MattermostClient, MattermostRequestError
from .retry import RetryPolicy
from .state import FileSnapshot, StateStore
from .watcher import (
    VaultWatcher,
    eligible_relative_path,
    is_draft_relative_path,
    scan_markdown_files,
)

LOGGER = logging.getLogger(__name__)


class Publisher(Protocol):
    def validate_auth(self) -> None: ...

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
        self._runtime_lock = threading.RLock()
        self._failed_vaults: dict[str, VaultConfig] = {}
        self._delivery_worker = DeliveryWorker(
            self._state,
            self._publisher,
            self._delivery_targets,
            RetryPolicy.from_config(config.mattermost),
            logger=self._logger,
        )
        self._started = False
        self._closed = False

    def start(self) -> None:
        if self._started:
            return
        self._publisher.validate_auth()
        self._logger.info("Mattermost 인증 검증 완료")

        failures: list[str] = []
        for vault in self.config.enabled_vaults:
            try:
                self._start_vault(vault)
            except Exception as exc:  # noqa: BLE001 - isolate each configured vault
                failures.append(vault.vault_name)
                if _is_retryable_start_failure(exc):
                    self._failed_vaults[vault.vault_name] = vault
                    self._logger.error(
                        "보관함 시작 실패, 재시도 예정 [%s]: %s",
                        vault.vault_name,
                        exc,
                    )
                else:
                    self._logger.error(
                        "보관함 시작 중단 [%s]: 설정을 수정한 뒤 재시작하세요: %s",
                        vault.vault_name,
                        exc,
                    )
        with self._runtime_lock:
            has_runtimes = bool(self._runtimes)
        if not has_runtimes:
            joined = ", ".join(failures) or "없음"
            raise RuntimeError(f"시작할 수 있는 보관함이 없습니다. 실패: {joined}")

        self._state.resume_pending()
        self._delivery_worker.start()
        self._delivery_worker.wake()
        self._started = True

    def stop(self) -> None:
        if self._closed:
            return
        with self._runtime_lock:
            runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            try:
                runtime.watcher.stop()
            except Exception as exc:  # noqa: BLE001 - best-effort shutdown
                self._logger.warning(
                    "보관함 감시 종료 실패 [%s]: %s", runtime.config.vault_name, exc
                )
        self._delivery_worker.stop()
        with self._runtime_lock:
            self._runtimes.clear()
        self._failed_vaults.clear()
        self._publisher.close()
        self._state.close()
        self._closed = True
        self._started = False

    def restart_failed_watchers(self) -> None:
        """Recreate failed or dead observers without disturbing healthy vaults."""
        for vault_name, vault in list(self._failed_vaults.items()):
            try:
                self._start_vault(vault)
            except Exception as exc:  # noqa: BLE001 - retry boundary for one vault
                if not _is_retryable_start_failure(exc):
                    self._failed_vaults.pop(vault_name, None)
                    self._logger.error(
                        "보관함 시작 재시도 중단 [%s]: 설정을 수정한 뒤 재시작하세요: %s",
                        vault_name,
                        exc,
                    )
                else:
                    self._logger.error(
                        "보관함 시작 재시도 실패 [%s]: %s", vault_name, exc
                    )
            else:
                self._failed_vaults.pop(vault_name, None)
                self._delivery_worker.wake()

        with self._runtime_lock:
            runtimes = list(self._runtimes.items())
        for vault_name, runtime in runtimes:
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
                watcher = self._new_watcher(runtime.config)
                watcher.start()
                observed_at = datetime.now(UTC)
                self._state.reconcile(
                    vault_name,
                    self._scannable_files(runtime.config),
                    observed_at=observed_at,
                    notify_after=observed_at
                    + timedelta(seconds=runtime.config.notification_quiet_seconds),
                )
                with self._runtime_lock:
                    self._runtimes[vault_name] = _VaultRuntime(
                        config=runtime.config,
                        channel_id=runtime.channel_id,
                        watcher=watcher,
                    )
                self._delivery_worker.wake()
            except Exception as exc:  # noqa: BLE001 - isolate watcher restarts
                if watcher is not None:
                    try:
                        watcher.stop()
                    except Exception as cleanup_exc:  # noqa: BLE001
                        self._logger.debug(
                            "실패한 감시기 정리 실패 [%s]: %s",
                            vault_name,
                            cleanup_exc,
                        )
                self._logger.error("보관함 감시 재시작 실패 [%s]: %s", vault_name, exc)

    def _start_vault(self, vault: VaultConfig) -> None:
        channel_id = self._publisher.channel_id_for(vault)
        watcher = self._new_watcher(vault)
        initialized = self._state.is_initialized(vault.vault_name)
        observed_at = datetime.now(UTC)
        notify_after = observed_at + timedelta(seconds=vault.notification_quiet_seconds)

        if initialized:
            watcher.start()
            try:
                self._state.reconcile(
                    vault.vault_name,
                    self._scannable_files(vault),
                    observed_at=observed_at,
                    notify_after=notify_after,
                )
            except Exception:
                watcher.stop()
                raise
        else:
            self._state.reconcile(
                vault.vault_name,
                self._scannable_files(vault),
                observed_at=observed_at,
                notify_after=notify_after,
            )
            watcher.start()

        with self._runtime_lock:
            self._runtimes[vault.vault_name] = _VaultRuntime(vault, channel_id, watcher)
        self._failed_vaults.pop(vault.vault_name, None)
        self._logger.info(
            "보관함 감시 시작: %s (%s)", vault.vault_name, vault.vault_path
        )

    def _new_watcher(self, vault: VaultConfig) -> VaultWatcher:
        return VaultWatcher(
            vault,
            lambda path: self._on_stable_file(vault, path),
            lambda source, destination: self._on_moved(vault, source, destination),
            lambda relative: self._state.mark_missing(vault.vault_name, relative),
            logger=self._logger,
        )

    def _on_stable_file(self, vault: VaultConfig, path: Path) -> None:
        relative_path = eligible_relative_path(vault, path)
        if relative_path is None:
            return
        if is_draft_relative_path(vault, relative_path):
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
            self._delivery_worker.wake()

    def _on_moved(
        self, vault: VaultConfig, source_relative: str, destination: Path
    ) -> None:
        destination_relative = eligible_relative_path(vault, destination)
        if destination_relative is None:
            self._state.mark_missing(vault.vault_name, source_relative)
            return
        try:
            stat = destination.stat()
        except OSError:
            return
        record = self._state.move_path(
            vault.vault_name,
            source_relative,
            FileSnapshot(destination_relative, stat.st_size, stat.st_mtime_ns),
        )
        if record is not None and record.status == "pending" and record.present:
            self._delivery_worker.wake()

    @staticmethod
    def _scannable_files(vault: VaultConfig) -> list[FileSnapshot]:
        return [
            snapshot
            for snapshot in scan_markdown_files(vault)
            if not is_draft_relative_path(vault, snapshot.relative_path)
        ]

    def _delivery_targets(self) -> dict[str, DeliveryTarget]:
        with self._runtime_lock:
            return {
                vault_name: DeliveryTarget(runtime.config, runtime.channel_id)
                for vault_name, runtime in self._runtimes.items()
            }


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _is_retryable_start_failure(error: Exception) -> bool:
    if isinstance(error, MattermostRequestError):
        return error.retryable
    return True
