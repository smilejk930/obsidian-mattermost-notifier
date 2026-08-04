from __future__ import annotations

from dataclasses import dataclass

from .config import MattermostConfig


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    immediate_retry_attempts: int
    base_seconds: float
    max_seconds: float
    jitter_ratio: float

    @classmethod
    def from_config(cls, config: MattermostConfig) -> RetryPolicy:
        return cls(
            immediate_retry_attempts=config.immediate_retry_attempts,
            base_seconds=config.retry_base_seconds,
            max_seconds=config.retry_max_seconds,
            jitter_ratio=config.retry_jitter_ratio,
        )

    def delay_seconds(
        self,
        failure_count: int,
        *,
        retry_after: float | None = None,
        random_value: float = 0.5,
    ) -> float:
        if failure_count <= 0:
            raise ValueError("failure_count는 1 이상이어야 합니다.")
        if not 0 <= random_value <= 1:
            raise ValueError("random_value는 0 이상 1 이하이어야 합니다.")

        if failure_count <= self.immediate_retry_attempts:
            exponent = min(failure_count - 1, 30)
            base_delay = min(self.max_seconds, self.base_seconds * (2**exponent))
        else:
            base_delay = self.max_seconds

        jitter_factor = 1 + self.jitter_ratio * ((2 * random_value) - 1)
        jittered_delay = max(0.0, base_delay * jitter_factor)
        if retry_after is None:
            return jittered_delay
        return max(jittered_delay, retry_after)
