from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from .config import VaultConfig
from .mattermost import MattermostRequestError
from .message import build_message, document_title
from .retry import RetryPolicy
from .state import DocumentRecord, StateStore

LOGGER = logging.getLogger(__name__)


class MessagePublisher(Protocol):
    def post_message(self, channel_id: str, message: str) -> str: ...


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    vault: VaultConfig
    channel_id: str


class DeliveryWorker:
    """Persistently schedules Mattermost posts outside watchdog event threads."""

    def __init__(
        self,
        state: StateStore,
        publisher: MessagePublisher,
        targets: Callable[[], Mapping[str, DeliveryTarget]],
        retry_policy: RetryPolicy,
        *,
        logger: logging.Logger | None = None,
        clock: Callable[[], datetime] | None = None,
        random_source: Callable[[], float] | None = None,
        max_workers: int = 4,
    ) -> None:
        self._state = state
        self._publisher = publisher
        self._targets = targets
        self._retry_policy = retry_policy
        self._logger = logger or LOGGER
        self._clock = clock or (lambda: datetime.now(UTC))
        self._random_source = random_source or random.random
        self._max_workers = max_workers
        self._condition = threading.Condition()
        self._generation = 0
        self._stopping = False
        self._started = False
        self._in_flight: set[tuple[str, str, int]] = set()
        self._vaults_in_flight: set[str] = set()
        self._executor: ThreadPoolExecutor | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="mattermost-delivery-worker",
            daemon=True,
        )

    def start(self) -> None:
        if self._started:
            return
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="mattermost-post",
        )
        self._started = True
        self._thread.start()

    def wake(self) -> None:
        with self._condition:
            self._generation += 1
            self._condition.notify_all()

    def stop(self) -> None:
        if not self._started:
            return
        with self._condition:
            self._stopping = True
            self._generation += 1
            self._condition.notify_all()
        self._thread.join()
        assert self._executor is not None
        self._executor.shutdown(wait=True, cancel_futures=False)
        self._executor = None
        self._started = False

    @property
    def is_alive(self) -> bool:
        return self._started and self._thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
                observed_generation = self._generation

            targets = dict(self._targets())
            now = self._utc_now()
            due = self._state.due_pending(targets.keys(), now=now)
            for record in due:
                if self._is_stopping():
                    return
                target = targets.get(record.vault_name)
                if target is not None:
                    self._submit(record, target)

            targets = dict(self._targets())
            next_retry = self._state.next_retry_time(targets.keys())
            now = self._utc_now()
            timeout = 60.0
            if next_retry is not None:
                timeout = max(0.0, min(60.0, (next_retry - now).total_seconds()))
            if due:
                timeout = max(0.05, timeout)

            with self._condition:
                if self._stopping:
                    return
                if self._generation != observed_generation:
                    continue
                self._condition.wait(timeout=timeout)

    def _submit(self, record: DocumentRecord, target: DeliveryTarget) -> None:
        key = (record.vault_name, record.relative_path, record.generation)
        with self._condition:
            if (
                self._stopping
                or key in self._in_flight
                or record.vault_name in self._vaults_in_flight
            ):
                return
            self._in_flight.add(key)
            self._vaults_in_flight.add(record.vault_name)
            executor = self._executor
        assert executor is not None
        try:
            executor.submit(self._attempt_and_release, key, record, target)
        except RuntimeError:
            with self._condition:
                self._in_flight.discard(key)
                self._vaults_in_flight.discard(record.vault_name)
            raise

    def _attempt_and_release(
        self,
        key: tuple[str, str, int],
        record: DocumentRecord,
        target: DeliveryTarget,
    ) -> None:
        try:
            self._attempt(record, target)
        except Exception as exc:  # noqa: BLE001 - keep worker pool alive
            self._logger.error(
                "전송 worker 처리 실패 [%s/%s, 예외=%s]",
                record.vault_name,
                record.relative_path,
                type(exc).__name__,
            )
        finally:
            with self._condition:
                self._in_flight.discard(key)
                self._vaults_in_flight.discard(record.vault_name)
            self.wake()

    def _attempt(self, candidate: DocumentRecord, target: DeliveryTarget) -> None:
        current = self._state.get(candidate.vault_name, candidate.relative_path)
        if (
            current is None
            or current.status != "pending"
            or not current.present
            or current.generation != candidate.generation
        ):
            return

        document = target.vault.vault_path / Path(current.relative_path)
        if not document.is_file():
            self._state.mark_missing(current.vault_name, current.relative_path)
            return
        message = build_message(
            vault_name=target.vault.vault_name,
            relative_path=current.relative_path,
            title=document_title(document),
            observed_at=current.first_observed_at,
        )
        try:
            post_id = self._publisher.post_message(target.channel_id, message)
        except MattermostRequestError as exc:
            self._record_failure(current, exc)
            return
        except Exception:  # noqa: BLE001 - publisher boundary; never log raw details
            self._record_unknown_failure(current)
            return

        try:
            self._state.mark_sent(
                current.vault_name,
                current.relative_path,
                post_id,
                generation=current.generation,
            )
        except KeyError:
            self._logger.info(
                "이전 generation의 Mattermost 응답을 무시했습니다: %s/%s",
                current.vault_name,
                current.relative_path,
            )

    def _record_failure(
        self, record: DocumentRecord, error: MattermostRequestError
    ) -> None:
        next_retry_at: datetime | None = None
        if error.retryable:
            delay = self._retry_policy.delay_seconds(
                record.attempt_count + 1,
                retry_after=error.retry_after,
                random_value=self._random_source(),
            )
            next_retry_at = self._utc_now() + timedelta(seconds=delay)
        try:
            self._state.mark_delivery_failure(
                record.vault_name,
                record.relative_path,
                generation=record.generation,
                error_code=error.code,
                next_retry_at=next_retry_at,
            )
        except KeyError:
            return
        if error.retryable:
            self._logger.warning(
                "Mattermost 전송 재시도 예약 [%s/%s, 오류=%s]",
                record.vault_name,
                record.relative_path,
                error.code,
            )
        else:
            self._logger.error(
                "Mattermost 전송 중단 [%s/%s, 오류=%s]: 설정을 수정한 뒤 재시작하세요.",
                record.vault_name,
                record.relative_path,
                error.code,
            )

    def _record_unknown_failure(self, record: DocumentRecord) -> None:
        delay = self._retry_policy.delay_seconds(
            record.attempt_count + 1,
            random_value=self._random_source(),
        )
        try:
            self._state.mark_delivery_failure(
                record.vault_name,
                record.relative_path,
                generation=record.generation,
                error_code="publisher_error",
                next_retry_at=self._utc_now() + timedelta(seconds=delay),
            )
        except KeyError:
            return
        self._logger.warning(
            "Mattermost 전송 재시도 예약 [%s/%s, 오류=publisher_error]",
            record.vault_name,
            record.relative_path,
        )

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("DeliveryWorker clock에는 timezone 정보가 필요합니다.")
        return value.astimezone(UTC)

    def _is_stopping(self) -> bool:
        with self._condition:
            return self._stopping
