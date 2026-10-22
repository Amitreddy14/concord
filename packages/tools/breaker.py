"""A circuit breaker per dependency.

Retry handles one call failing. A breaker handles a dependency being down: it
stops the agent spending its retry budget, and the merchant's patience, on a
service that is not answering anyone.

Per dependency, not global. The payments ledger being down must not stop the
agent reading the knowledge base to explain the outage — degraded is better
than dead, and that is only possible if the failures are tracked separately.

Three states, the usual ones:

``closed``
    Calls pass. Consecutive failures are counted; a success resets the count,
    because five failures spread over an hour are not an outage.

``open``
    Calls are refused immediately without touching the network. After the
    cooldown the breaker moves to half-open.

``half_open``
    One trial call is allowed. Success closes the breaker; failure reopens it
    and restarts the cooldown. Exactly one, so a recovering dependency is not
    hit by everything that queued up while it was down.

Time is injected rather than read from the clock, so the tests exercise real
transitions instead of sleeping through them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(Exception):
    """Raised instead of calling a dependency whose breaker is open."""

    def __init__(self, dependency: str, retry_at: datetime) -> None:
        super().__init__(
            f"circuit for {dependency!r} is open; not retrying until {retry_at.isoformat()}"
        )
        self.dependency = dependency
        self.retry_at = retry_at


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class CircuitBreaker:
    dependency: str
    failure_threshold: int = 5
    cooldown: timedelta = timedelta(seconds=30)
    clock: Callable[[], datetime] = _now

    _state: BreakerState = field(default=BreakerState.CLOSED, init=False)
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: datetime | None = field(default=None, init=False)
    _half_open_in_flight: bool = field(default=False, init=False)

    @property
    def state(self) -> BreakerState:
        """The current state, accounting for an elapsed cooldown."""
        if (
            self._state is BreakerState.OPEN
            and self._opened_at is not None
            and self.clock() - self._opened_at >= self.cooldown
        ):
            self._state = BreakerState.HALF_OPEN
            self._half_open_in_flight = False
        return self._state

    @property
    def retry_at(self) -> datetime:
        if self._opened_at is None:
            return self.clock()
        return self._opened_at + self.cooldown

    def before_call(self) -> None:
        """Raise if the call must not be made. Called before every attempt."""
        state = self.state
        if state is BreakerState.OPEN:
            raise CircuitOpen(self.dependency, self.retry_at)
        if state is BreakerState.HALF_OPEN:
            if self._half_open_in_flight:
                # Someone else is already probing. Everything behind them
                # waits rather than piling onto a dependency that is only
                # just coming back.
                raise CircuitOpen(self.dependency, self.retry_at)
            self._half_open_in_flight = True

    def record_success(self) -> None:
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None
        self._half_open_in_flight = False

    def record_failure(self) -> None:
        """Count a failure, opening the breaker at the threshold.

        A failure during a half-open trial reopens immediately rather than
        incrementing: the trial existed to answer one question, and it did.
        """
        if self._state is BreakerState.HALF_OPEN:
            self._open()
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._open()

    def _open(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self.clock()
        self._half_open_in_flight = False

    def reset(self) -> None:
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None
        self._half_open_in_flight = False


@dataclass
class BreakerRegistry:
    """One breaker per dependency, created on first use."""

    failure_threshold: int = 5
    cooldown: timedelta = timedelta(seconds=30)
    clock: Callable[[], datetime] = _now
    _breakers: dict[str, CircuitBreaker] = field(default_factory=dict)

    def for_dependency(self, dependency: str) -> CircuitBreaker:
        if dependency not in self._breakers:
            self._breakers[dependency] = CircuitBreaker(
                dependency=dependency,
                failure_threshold=self.failure_threshold,
                cooldown=self.cooldown,
                clock=self.clock,
            )
        return self._breakers[dependency]

    def states(self) -> dict[str, BreakerState]:
        return {name: breaker.state for name, breaker in self._breakers.items()}

    def reset_all(self) -> None:
        for breaker in self._breakers.values():
            breaker.reset()
