"""The dead-letter queue: runs that failed terminally, kept where someone looks.

A run that fails has to end up somewhere a person will find it. The default —
a log line and a run row marked `failed` — satisfies the letter of that and
not the spirit: nobody greps logs for the failures they do not know happened,
and a status column does not say what to do about it.

So a dead letter carries three things a status cannot:

**What was established before the failure.** The triage, the resolved payment,
the decision, the proposal. A replay starts from these rather than from the
ticket, so re-running does not re-do the reads that already succeeded.

**Whether anything happened upstream.** A refund that failed with no effect is
a different artefact from one whose effect is unknown, and they want different
handling: the first can be replayed freely, the second must not be replayed at
all until a human has checked the gateway. The queue records which, and
`replayable` refuses the second.

**Why nobody could proceed.** The classified failure, not a stringified
exception.

Replay is deliberately not automatic. An operator decides, because the
failures that reach here are the ones where the right action depends on facts
outside the system — whether the gateway's incident is over, whether the
merchant has already been refunded by hand. A queue that drained itself would
be a retry loop with extra storage.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from packages.tools.reconcile import Resolution


class LetterStatus(StrEnum):
    OPEN = "open"
    REPLAYED = "replayed"
    RESOLVED = "resolved"
    """Closed by a human who handled it outside the system."""
    ABANDONED = "abandoned"


#: Failures whose effect upstream is unknown. A run that failed this way must
#: not be replayed until a person has established what happened, because the
#: replay would re-attempt a mutation that may already have landed.
UNSAFE_TO_REPLAY = frozenset({"refund_indeterminate_needs_human"})


@dataclass(frozen=True, slots=True)
class DeadLetter:
    letter_id: str
    run_id: str
    ticket_id: str
    tenant_id: str
    merchant_id: str
    failure: str
    failed_at: datetime
    state: dict[str, Any]
    """What the run had established. The replay's starting point."""
    reconciliation: dict[str, Any] | None = None
    status: LetterStatus = LetterStatus.OPEN
    handled_by: str | None = None
    handled_at: datetime | None = None
    note: str | None = None
    replay_run_id: str | None = None

    @property
    def effect_is_unknown(self) -> bool:
        """Whether anything may have happened upstream that nobody has confirmed."""
        if self.failure in UNSAFE_TO_REPLAY:
            return True
        resolution = (self.reconciliation or {}).get("resolution")
        return resolution == str(Resolution.UNKNOWN)

    @property
    def replayable(self) -> bool:
        """Whether replaying this is safe without a human checking first.

        Open and demonstrably without effect. A letter whose effect is
        unknown is refused here rather than warned about, because a warning
        on a replay button is a warning people click through.
        """
        return self.status is LetterStatus.OPEN and not self.effect_is_unknown

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = str(self.status)
        payload["replayable"] = self.replayable
        payload["effect_is_unknown"] = self.effect_is_unknown
        for key in ("failed_at", "handled_at"):
            value = payload.get(key)
            payload[key] = value.isoformat() if isinstance(value, datetime) else None
        # The captured state can be large and is for the replay rather than
        # for a queue listing; the summary names what it holds instead.
        payload["state_keys"] = sorted(self.state)
        del payload["state"]
        return payload


class DeadLetterError(Exception):
    """An operation that cannot be applied to this letter."""


@runtime_checkable
class DeadLetterQueue(Protocol):
    def add(self, letter: DeadLetter) -> DeadLetter: ...

    def get(self, letter_id: str) -> DeadLetter | None: ...

    def open_letters(self, *, tenant_id: str | None = None) -> list[DeadLetter]: ...

    def mark(
        self,
        letter_id: str,
        *,
        status: LetterStatus,
        handled_by: str,
        note: str | None = None,
        replay_run_id: str | None = None,
        now: datetime | None = None,
    ) -> DeadLetter: ...


def letter_id_for(run_id: str) -> str:
    """One letter per run, derived rather than generated.

    A run that fails, gets replayed and fails again produces a second run id
    and so a second letter. A run that is somehow reported twice produces the
    same id and is deduplicated, which matters because the alternative is an
    operator working the same failure twice.
    """
    return f"dlq_{run_id}"


@dataclass
class InMemoryDeadLetterQueue:
    _rows: dict[str, DeadLetter] = field(default_factory=dict)

    def add(self, letter: DeadLetter) -> DeadLetter:
        existing = self._rows.get(letter.letter_id)
        if existing is not None:
            return existing
        self._rows[letter.letter_id] = letter
        return letter

    def get(self, letter_id: str) -> DeadLetter | None:
        return self._rows.get(letter_id)

    def open_letters(self, *, tenant_id: str | None = None) -> list[DeadLetter]:
        rows = [r for r in self._rows.values() if r.status is LetterStatus.OPEN]
        if tenant_id is not None:
            rows = [r for r in rows if r.tenant_id == tenant_id]
        return sorted(rows, key=lambda r: (r.failed_at, r.letter_id))

    def mark(
        self,
        letter_id: str,
        *,
        status: LetterStatus,
        handled_by: str,
        note: str | None = None,
        replay_run_id: str | None = None,
        now: datetime | None = None,
    ) -> DeadLetter:
        row = self._rows.get(letter_id)
        if row is None:
            raise DeadLetterError(f"no dead letter {letter_id!r}")
        if row.status is not LetterStatus.OPEN:
            raise DeadLetterError(
                f"dead letter {letter_id} is already {row.status}; "
                "a handled failure is not handled twice"
            )
        if status is LetterStatus.REPLAYED and not row.replayable:
            raise DeadLetterError(
                f"dead letter {letter_id} is not safe to replay: its effect upstream is "
                "unknown. Establish what happened at the gateway, then close it as "
                "resolved or abandoned."
            )
        moved = replace(
            row,
            status=status,
            handled_by=handled_by,
            note=note,
            replay_run_id=replay_run_id,
            handled_at=now or datetime.now(UTC),
        )
        self._rows[letter_id] = moved
        return moved

    def all(self) -> Iterable[DeadLetter]:
        return list(self._rows.values())

    def clear(self) -> None:
        self._rows.clear()


def from_failed_run(
    *,
    run_id: str,
    ticket_id: str,
    tenant_id: str,
    merchant_id: str,
    failure: str,
    state: dict[str, Any],
    now: datetime | None = None,
) -> DeadLetter:
    """Build a letter from a run that has just failed."""
    return DeadLetter(
        letter_id=letter_id_for(run_id),
        run_id=run_id,
        ticket_id=ticket_id,
        tenant_id=tenant_id,
        merchant_id=merchant_id,
        failure=failure,
        failed_at=now or datetime.now(UTC),
        state=dict(state),
        reconciliation=state.get("reconciliation"),
    )
