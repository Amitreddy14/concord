"""The append-only run ledger.

Every model call, tool call, policy decision, approval and error becomes a
step. Steps are never updated and never deleted — a correction is a new step,
because a reviewer asking "why was that refunded?" needs what actually
happened, not a tidied summary of it.

Two things are deliberately separated. A ``RunStep`` carries digests of its
input and output rather than the payloads, so scanning a run's history stays
cheap; the payloads live in their own map, fetched only when something is
being examined closely. Phase 5 moves both to Postgres behind the same
protocol, which is why the protocol exists now rather than then.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol


class StepKind(StrEnum):
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    POLICY_DECISION = "policy_decision"
    APPROVAL = "approval"
    ERROR = "error"
    COMPENSATION = "compensation"


class StepOutcome(StrEnum):
    STARTED = "started"
    SUCCESS = "success"
    ERROR = "error"
    REFUSED = "refused"


def digest(payload: Any) -> str:
    """A stable hash of a payload, for the ledger's input/output fields."""
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.blake2b(material.encode(), digest_size=16).hexdigest()


@dataclass(frozen=True, slots=True)
class RunStep:
    run_id: str
    seq: int
    node: str
    kind: StepKind
    outcome: StepOutcome
    started_at: datetime
    ended_at: datetime | None = None
    tool: str | None = None
    scope: str | None = None
    policy_rule_id: str | None = None
    attempt: int = 1
    input_digest: str | None = None
    output_digest: str | None = None
    error_code: str | None = None
    detail: str | None = None

    @property
    def duration_ms(self) -> int | None:
        if self.ended_at is None:
            return None
        return int((self.ended_at - self.started_at).total_seconds() * 1000)


class Ledger(Protocol):
    """What the agent writes its history to.

    In phase 5 this is Postgres alongside the LangGraph checkpointer. The
    protocol is defined now so the tool layer is written against it rather
    than against whatever happens to be convenient today.
    """

    def append(self, step: RunStep, *, payloads: dict[str, Any] | None = None) -> RunStep: ...

    def steps(self, run_id: str) -> list[RunStep]: ...

    def payload(self, run_id: str, seq: int, which: str) -> Any: ...


@dataclass
class InMemoryLedger:
    """The development and test implementation."""

    _steps: dict[str, list[RunStep]] = field(default_factory=dict)
    _payloads: dict[tuple[str, int, str], Any] = field(default_factory=dict)

    def next_seq(self, run_id: str) -> int:
        return len(self._steps.get(run_id, [])) + 1

    def append(self, step: RunStep, *, payloads: dict[str, Any] | None = None) -> RunStep:
        self._steps.setdefault(step.run_id, []).append(step)
        for which, value in (payloads or {}).items():
            self._payloads[(step.run_id, step.seq, which)] = value
        return step

    def steps(self, run_id: str) -> list[RunStep]:
        return list(self._steps.get(run_id, []))

    def payload(self, run_id: str, seq: int, which: str) -> Any:
        return self._payloads.get((run_id, seq, which))

    # -- convenience for assertions and the reviewer console --------------

    def tool_calls(self, run_id: str, tool: str | None = None) -> list[RunStep]:
        return [
            s
            for s in self.steps(run_id)
            if s.kind is StepKind.TOOL_CALL and (tool is None or s.tool == tool)
        ]

    def scopes_used(self, run_id: str) -> set[str]:
        """Every scope this run actually exercised.

        S10 asserts that the set equals the tenant's configuration exactly: a
        successful injection must not be able to widen it.
        """
        return {s.scope for s in self.steps(run_id) if s.scope} - {None}

    def clear(self) -> None:
        self._steps.clear()
        self._payloads.clear()


@contextmanager
def record_step(
    ledger: InMemoryLedger,
    *,
    run_id: str,
    node: str,
    kind: StepKind,
    tool: str | None = None,
    scope: str | None = None,
    attempt: int = 1,
    request: Any = None,
) -> Iterator[dict[str, Any]]:
    """Write a step before the work and a second step after it.

    Two steps rather than one mutable row. A single row updated on completion
    would lose the fact that the call was started at all when the process dies
    mid-call — which is exactly the situation phase 7's reconciliation pass
    has to detect and resolve.

    The caller fills ``result["output"]`` on success; an exception is recorded
    as an error step and re-raised.
    """
    started = datetime.now(UTC)
    seq = ledger.next_seq(run_id)
    ledger.append(
        RunStep(
            run_id=run_id,
            seq=seq,
            node=node,
            kind=kind,
            outcome=StepOutcome.STARTED,
            started_at=started,
            tool=tool,
            scope=scope,
            attempt=attempt,
            input_digest=digest(request) if request is not None else None,
        ),
        payloads={"input": request} if request is not None else None,
    )

    box: dict[str, Any] = {"output": None, "error_code": None, "detail": None}
    try:
        yield box
    except BaseException as error:
        ledger.append(
            RunStep(
                run_id=run_id,
                seq=ledger.next_seq(run_id),
                node=node,
                kind=StepKind.ERROR,
                outcome=StepOutcome.ERROR,
                started_at=started,
                ended_at=datetime.now(UTC),
                tool=tool,
                scope=scope,
                attempt=attempt,
                input_digest=digest(request) if request is not None else None,
                error_code=box.get("error_code") or type(error).__name__,
                detail=str(error)[:500],
            )
        )
        raise
    else:
        ledger.append(
            RunStep(
                run_id=run_id,
                seq=ledger.next_seq(run_id),
                node=node,
                kind=kind,
                outcome=StepOutcome.SUCCESS,
                started_at=started,
                ended_at=datetime.now(UTC),
                tool=tool,
                scope=scope,
                attempt=attempt,
                input_digest=digest(request) if request is not None else None,
                output_digest=digest(box["output"]) if box["output"] is not None else None,
            ),
            payloads={"output": box["output"]} if box["output"] is not None else None,
        )
