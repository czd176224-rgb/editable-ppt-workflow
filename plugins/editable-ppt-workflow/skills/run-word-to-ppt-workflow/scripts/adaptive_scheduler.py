"""Bounded adaptive scheduling for independent Word-page work."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Sequence


PROFILE_CONCURRENCY = {"balanced": 2, "quality": 2, "speed": 3}


def _status_code(error: BaseException) -> int | None:
    value = getattr(error, "status_code", getattr(error, "code", None))
    return value if type(value) is int else None


def should_retry(error: BaseException) -> bool:
    status = _status_code(error)
    return status == 429 or (status is not None and 500 <= status <= 599) or isinstance(
        error, (ConnectionError, TimeoutError)
    ) or getattr(error, "network", False) is True


def jittered_retry_delay(attempt: int, *, jitter: float) -> float:
    if type(attempt) is not int or attempt < 0:
        raise ValueError("attempt must be a non-negative integer")
    if not isinstance(jitter, (int, float)) or not 0.0 <= float(jitter) <= 1.0:
        raise ValueError("jitter must be between 0 and 1")
    base = min(30.0, float(2 ** attempt))
    return base * (0.75 + 0.5 * float(jitter))


ReadyState = Literal["queued", "repair", "accepted"]


@dataclass(frozen=True)
class PageJob:
    page_number: int
    complexity_weight: int
    state: ReadyState


@dataclass(frozen=True)
class RoundOutcome:
    successes: int = 0
    completed: int = 0
    expected: int = 0
    failures: int = 0
    rate_limits: int = 0
    timeouts: int = 0
    infrastructure_errors: int = 0


@dataclass(frozen=True)
class SchedulerSnapshot:
    concurrency: int
    launch_capacity: int
    window_size: int
    active_count: int
    active_weight: int
    queued_depth: int
    repair_depth: int
    accepted_depth: int


class AdaptiveScheduler:
    minimum_concurrency = 1
    maximum_concurrency = 8
    maximum_active_weight = 8

    def __init__(
        self,
        page_count: int,
        initial_concurrency: int | None = None,
        maximum_concurrency: int | None = None,
    ):
        if type(page_count) is not int or page_count < 1:
            raise ValueError("page_count must be a positive integer")
        configured_max = self.maximum_concurrency if maximum_concurrency is None else maximum_concurrency
        if type(configured_max) is not int or not self.minimum_concurrency <= configured_max <= self.maximum_concurrency:
            raise ValueError("maximum_concurrency must be between 1 and 8")
        self.configured_maximum_concurrency = configured_max
        default = min(configured_max, 2 if page_count <= 10 else (3 if page_count <= 40 else 4))
        concurrency = default if initial_concurrency is None else initial_concurrency
        if type(concurrency) is not int or not self.minimum_concurrency <= concurrency <= configured_max:
            raise ValueError("initial_concurrency must be within the configured concurrency bound")
        self.page_count = page_count
        self._concurrency = concurrency
        self._launch_capacity = concurrency
        self._active_count = 0
        self._active_weight = 0
        self._depths = {"queued": 0, "repair": 0, "accepted": 0}
        self.last_trigger_code = "initial"
        self.completed_receipts: dict[int, dict[str, Any]] = {}

    @classmethod
    def for_profile(cls, profile: str, *, page_count: int = 1) -> "AdaptiveScheduler":
        try:
            concurrency = PROFILE_CONCURRENCY[profile]
        except KeyError as exc:
            raise ValueError("profile must be quality, balanced, or speed") from exc
        return cls(
            page_count=page_count,
            initial_concurrency=concurrency,
            maximum_concurrency=concurrency,
        )

    @property
    def active_concurrency(self) -> int:
        return self._concurrency

    def note_failure(self, error: BaseException) -> bool:
        if not should_retry(error):
            return False
        if _status_code(error) == 429:
            self._concurrency = 1
            self._launch_capacity = 1
            self.last_trigger_code = "rate_limit"
        return True

    def mark_completed(self, page_number: int, receipt: Mapping[str, Any]) -> None:
        if type(page_number) is not int or page_number < 1:
            raise ValueError("page_number must be positive")
        self.completed_receipts[page_number] = dict(receipt)

    def pending_pages(self, page_numbers: Sequence[int]) -> list[int]:
        return [number for number in page_numbers if number not in self.completed_receipts]

    def run_page(
        self,
        page_number: int,
        action: Callable[[], Mapping[str, Any]],
        *,
        max_attempts: int = 3,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> dict[str, Any]:
        """Run one not-yet-completed page with narrowly classified retries."""
        if page_number in self.completed_receipts:
            return dict(self.completed_receipts[page_number])
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        sleeper = sleep if sleep is not None else time.sleep
        jitter_source = jitter if jitter is not None else random.random
        for attempt in range(max_attempts):
            try:
                receipt = dict(action())
            except Exception as exc:
                if attempt + 1 >= max_attempts or not self.note_failure(exc):
                    raise
                sleeper(jittered_retry_delay(attempt, jitter=float(jitter_source())))
                continue
            self.mark_completed(page_number, receipt)
            return receipt
        raise RuntimeError("unreachable retry state")

    def run_transient(
        self,
        action: Callable[[], Any],
        *,
        max_attempts: int = 3,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> Any:
        """Retry one provider operation without treating it as another candidate."""
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        sleeper = sleep if sleep is not None else time.sleep
        jitter_source = jitter if jitter is not None else random.random
        for attempt in range(max_attempts):
            try:
                return action()
            except Exception as exc:
                if attempt + 1 >= max_attempts or not self.note_failure(exc):
                    raise
                sleeper(jittered_retry_delay(attempt, jitter=float(jitter_source())))
        raise RuntimeError("unreachable retry state")

    @property
    def window_size(self) -> int:
        if self.page_count > 100:
            return min(12, max(8, self._concurrency * 2))
        return min(12, self.page_count)

    def snapshot(self) -> SchedulerSnapshot:
        return SchedulerSnapshot(
            concurrency=self._concurrency,
            launch_capacity=self._launch_capacity,
            window_size=self.window_size,
            active_count=self._active_count,
            active_weight=self._active_weight,
            queued_depth=self._depths["queued"],
            repair_depth=self._depths["repair"],
            accepted_depth=self._depths["accepted"],
        )

    @staticmethod
    def _validated(job: PageJob) -> PageJob:
        if job.state not in {"queued", "repair", "accepted"}:
            raise ValueError("page job state must be queued, repair, or accepted")
        if type(job.page_number) is not int or job.page_number < 1:
            raise ValueError("page_number must be positive")
        if job.complexity_weight not in {1, 2, 3}:
            raise ValueError("complexity_weight must be 1, 2, or 3")
        return job

    def next_batch(
        self,
        jobs: Sequence[PageJob],
        *,
        active_count: int = 0,
        active_weight: int = 0,
    ) -> list[int]:
        """Select only launchable work, preferring accepted and fresh queued pages.

        Repair remains eligible whenever capacity remains, but can never consume a
        slot ahead of an accepted or never-attempted page.
        """
        if type(active_count) is not int or active_count < 0:
            raise ValueError("active_count must not be negative")
        if type(active_weight) is not int or active_weight < 0:
            raise ValueError("active_weight must not be negative")
        validated = [self._validated(job) for job in jobs]
        numbers = [job.page_number for job in validated]
        if len(numbers) != len(set(numbers)):
            raise ValueError("page jobs must be unique")
        self._active_count = active_count
        self._active_weight = active_weight
        self._depths = {
            state: sum(job.state == state for job in validated)
            for state in ("queued", "repair", "accepted")
        }
        slots = max(0, self._concurrency - active_count)
        weight_budget = max(0, self.maximum_active_weight - active_weight)
        ordered = sorted(
            validated,
            key=lambda job: (
                {"accepted": 0, "queued": 1, "repair": 2}[job.state],
                -job.complexity_weight,
                job.page_number,
            ),
        )
        selected: list[int] = []
        selected_weight = 0
        for job in ordered:
            if len(selected) >= slots:
                break
            if selected_weight + job.complexity_weight > weight_budget:
                continue
            selected.append(job.page_number)
            selected_weight += job.complexity_weight
        self._launch_capacity = len(selected)
        return selected

    def record_round(self, outcome: RoundOutcome) -> SchedulerSnapshot:
        values = (
            outcome.successes,
            outcome.completed,
            outcome.expected,
            outcome.failures,
            outcome.rate_limits,
            outcome.timeouts,
            outcome.infrastructure_errors,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("round outcome counts must be non-negative integers")
        if outcome.rate_limits:
            self._concurrency = self.minimum_concurrency
            self.last_trigger_code = "rate_limit"
        elif outcome.timeouts:
            self._concurrency = max(self.minimum_concurrency, self._concurrency // 2)
            self.last_trigger_code = "timeout"
        elif outcome.infrastructure_errors:
            self._concurrency = max(self.minimum_concurrency, self._concurrency // 2)
            self.last_trigger_code = "infrastructure_error"
        elif outcome.failures:
            self._concurrency = max(self.minimum_concurrency, self._concurrency // 2)
            self.last_trigger_code = "failure"
        elif outcome.expected > 0 and outcome.completed == outcome.expected == outcome.successes:
            self._concurrency = min(self.configured_maximum_concurrency, self._concurrency + 1)
            self.last_trigger_code = "full_success"
        else:
            self.last_trigger_code = "no_change"
        self._active_count = 0
        self._active_weight = 0
        self._launch_capacity = self._concurrency
        return self.snapshot()
