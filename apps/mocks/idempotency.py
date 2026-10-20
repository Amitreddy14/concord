"""Per-service idempotency store.

The contract promises that a repeated ``Idempotency-Key`` with identical
arguments returns the original response and performs no second effect, while
the same key with different arguments is a conflict. S09 depends on exactly
that: the first refund attempt is accepted upstream and then times out at the
client, and the retry must return the original refund rather than create a
second one.

Two details matter and are easy to get wrong.

The stored fingerprint is of the *request*, not just the key. Returning a
cached response for a key whose arguments have changed would hide a caller bug
behind a success, so that case is a 409.

An entry is reserved before the handler runs and completed after. A concurrent
retry arriving while the first is still in flight therefore sees a reservation
rather than an absence, and is told the request is in progress instead of being
allowed to perform the effect a second time.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.shared.problem import IDEMPOTENCY_KEY_REUSED, STATE_CONFLICT, ProblemError

# Long enough to cover a retry after a client timeout, short enough that the
# store is not a memory leak in a long-lived process.
DEFAULT_TTL = timedelta(hours=24)


@dataclass(slots=True)
class Entry:
    key: str
    fingerprint: str
    created_at: datetime
    status_code: int | None = None
    response: dict[str, Any] | None = None

    @property
    def in_flight(self) -> bool:
        return self.response is None


def fingerprint(method: str, path: str, body: Any) -> str:
    """A stable hash of the request this key was first used for.

    Canonical JSON with sorted keys, so a body that differs only in key order
    is recognised as the same request rather than as a conflicting one.
    """
    material = json.dumps(
        {"method": method.upper(), "path": path, "body": body},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.blake2b(material.encode(), digest_size=16).hexdigest()


class IdempotencyStore:
    def __init__(self, ttl: timedelta = DEFAULT_TTL) -> None:
        self._entries: dict[str, Entry] = {}
        self._ttl = ttl

    def clear(self) -> None:
        self._entries.clear()

    def _expire(self, now: datetime) -> None:
        cutoff = now - self._ttl
        for key in [k for k, e in self._entries.items() if e.created_at < cutoff]:
            del self._entries[key]

    def reserve(self, key: str, method: str, path: str, body: Any) -> Entry | None:
        """Claim a key before performing an effect.

        Returns the completed entry when this key has already been used with
        the same arguments, in which case the caller replays it and performs no
        effect. Returns ``None`` when the key is new and the caller should
        proceed. Raises when the key was reused with different arguments, or
        when an identical request is still in flight.
        """
        now = datetime.now(UTC)
        self._expire(now)
        current = fingerprint(method, path, body)
        existing = self._entries.get(key)

        if existing is None:
            self._entries[key] = Entry(key=key, fingerprint=current, created_at=now)
            return None

        if existing.fingerprint != current:
            raise ProblemError(
                IDEMPOTENCY_KEY_REUSED,
                f"idempotency key {key} was already used for a different request; "
                "a key is derived from the arguments and must not be reused across them",
                instance=key,
            )

        if existing.in_flight:
            raise ProblemError(
                STATE_CONFLICT,
                f"a request with idempotency key {key} is still in progress",
                instance=key,
            )

        return existing

    def complete(self, key: str, status_code: int, response: dict[str, Any]) -> None:
        entry = self._entries.get(key)
        if entry is None:  # pragma: no cover - reserve always precedes complete
            return
        entry.status_code = status_code
        entry.response = response

    def release(self, key: str) -> None:
        """Drop a reservation whose handler failed, so a retry may proceed.

        A handler that raised performed no effect, so holding the key would
        wrongly block a legitimate retry.
        """
        entry = self._entries.get(key)
        if entry is not None and entry.in_flight:
            del self._entries[key]
