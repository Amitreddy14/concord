"""Bounded retry with deterministic jitter.

Jitter is normally random, to stop a thundering herd of clients retrying in
lockstep. Here it is a hash of (run_id, node, tool, attempt) instead, which
keeps the herd-spreading property — two different runs get different delays —
while making a single run's timing reproducible.

That matters because phase 7 replays runs from their checkpoints. A replay
whose backoff differs from the original is not a replay of the same run, and
a flaky-looking test that depends on `random` is worse than no jitter at all.

The ceiling is on total elapsed delay rather than only on attempt count. Five
attempts with exponential backoff can take a long time, and a merchant waiting
on a chat reply is not helped by a tool layer that is still being patient.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum


class RetryClass(StrEnum):
    """How hard to try, by what the tool does."""

    NONE = "none"
    """No retry. For calls whose failure the caller must see immediately."""

    READ = "read"
    """Reads are cheap and side-effect free, so they get the most attempts."""

    MUTATION = "mutation"
    """Fewer attempts, and only safe because every mutation carries a key."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    base_delay_ms: int
    max_delay_ms: int
    max_total_delay_ms: int
    jitter_fraction: float = 0.25
    """Jitter as a fraction of the computed delay, applied deterministically."""

    @classmethod
    def for_class(cls, retry_class: RetryClass) -> RetryPolicy:
        match retry_class:
            case RetryClass.NONE:
                return cls(1, 0, 0, 0, 0.0)
            case RetryClass.READ:
                return cls(
                    max_attempts=4,
                    base_delay_ms=100,
                    max_delay_ms=2_000,
                    max_total_delay_ms=6_000,
                )
            case RetryClass.MUTATION:
                # Three attempts, not four. A mutation that has failed twice
                # is more likely to be wrong than unlucky, and every extra
                # attempt widens the window in which an accepted-then-timed-out
                # effect sits unreconciled.
                return cls(
                    max_attempts=3,
                    base_delay_ms=250,
                    max_delay_ms=4_000,
                    max_total_delay_ms=8_000,
                )


def _jitter_ratio(run_id: str, node: str, tool: str, attempt: int) -> float:
    """A stable value in [0, 1) for this exact retry.

    Same construction as the fault injector, for the same reason: a hash of
    the identifying tuple rather than a stateful RNG, so one call's timing
    does not depend on how many other calls happened first.
    """
    material = f"{run_id}\x1f{node}\x1f{tool}\x1f{attempt}".encode()
    digest = hashlib.blake2b(material, digest_size=4).digest()
    return int.from_bytes(digest, "big") / 2**32


def delay_for_attempt(
    policy: RetryPolicy,
    attempt: int,
    *,
    run_id: str,
    node: str,
    tool: str,
    retry_after_s: int | None = None,
) -> int:
    """Milliseconds to wait before ``attempt`` (1-based; attempt 1 waits 0).

    A ``Retry-After`` from the service wins over the computed backoff. The
    dependency knows when it will be ready and we do not, and ignoring it is
    how a rate limit becomes a longer rate limit.
    """
    if attempt <= 1:
        return 0
    if retry_after_s is not None:
        return min(retry_after_s * 1000, policy.max_delay_ms)

    exponential = policy.base_delay_ms * (2 ** (attempt - 2))
    capped = min(exponential, policy.max_delay_ms)
    ratio = _jitter_ratio(run_id, node, tool, attempt)
    # Jitter downward only, so the cap stays a cap.
    jittered = capped * (1 - policy.jitter_fraction * ratio)
    return int(jittered)


def schedule(
    policy: RetryPolicy, *, run_id: str, node: str, tool: str
) -> list[int]:
    """The full delay schedule, truncated by the total-delay ceiling.

    Returned as a list so a caller — or a test — can see the whole plan
    rather than discovering it one sleep at a time.
    """
    delays: list[int] = []
    total = 0
    for attempt in range(2, policy.max_attempts + 1):
        delay = delay_for_attempt(policy, attempt, run_id=run_id, node=node, tool=tool)
        if total + delay > policy.max_total_delay_ms:
            break
        delays.append(delay)
        total += delay
    return delays
