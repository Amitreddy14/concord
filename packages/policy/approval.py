"""Pending approvals: the index LangGraph cannot give us.

A run that stops for a human is paused inside the graph, and the checkpoint is
the truth about where. But a checkpoint is addressed by thread id, which means
it can answer "where is ticket T-1 paused?" and cannot answer either of the
two questions a reviewer console actually asks:

* what is waiting for a human, across every ticket?
* what has been waiting too long?

Scanning every thread to answer the first is not a query, and the second has no
answer at all inside LangGraph: a frozen thread stays frozen forever. Nothing
expires it, nothing escalates it, and a refund a merchant is waiting on sits
there indefinitely looking, from the inside, exactly like a healthy pause. ADR
0001 took the durable pause and accepted the obligation that comes with it —
this module is that obligation.

So: an index, written beside the checkpoint. It holds no authority. The
decision it carries was made by the policy engine, the pause it points at
lives in the checkpoint, and the resume is performed against the graph. If the
index and the checkpoint ever disagree, the checkpoint is right; the index is a
way of finding threads, not a second source of truth.

Two properties the implementation has to have, both learned from how the graph
behaves rather than chosen for elegance:

**Writes are idempotent.** LangGraph re-executes an interrupted node from its
first line when the run resumes. Everything before the ``interrupt()`` call
therefore runs at least twice, so a plain insert would produce a second
approval row on every resume. Requests are keyed by ``(run_id, fingerprint)``
and upserted.

**Terminal states are terminal.** A reviewer's decision, once recorded, is not
overwritten by a later sweep or a second reviewer. The expiry sweep moves only
``pending`` rows, and ``decide`` refuses a row that already has an answer —
the race between a reviewer clicking approve and the sweep expiring the same
request is real, and whoever gets there first wins.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self is not ApprovalStatus.PENDING


#: Statuses that let the graph proceed with a mutation. ``MODIFIED`` is in the
#: set because a reviewer who edited the amount still authorised a refund —
#: what changed is the proposal, not whether one happens.
PROCEEDING = frozenset({ApprovalStatus.APPROVED, ApprovalStatus.MODIFIED})


class ApprovalError(Exception):
    """A decision that cannot be applied to this request."""


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """One proposal waiting for a human.

    The decision and proposal are stored as plain payloads rather than as the
    objects that produced them. A reviewer opening this next week must see the
    rule id, the inputs and the amount *as they were evaluated*, not as a
    re-evaluation against today's tenant file would render them. A stored
    decision that silently re-derives is a stored decision that can lie.
    """

    approval_id: str
    run_id: str
    ticket_id: str
    thread_id: str
    tenant_id: str
    merchant_id: str
    fingerprint: str
    decision: dict[str, Any]
    proposal: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    status: ApprovalStatus = ApprovalStatus.PENDING

    # Set when a human answers.
    decided_at: datetime | None = None
    reviewer: str | None = None
    reviewer_note: str | None = None
    modified_proposal: dict[str, Any] | None = None
    """The reviewer's edit, when they changed the proposal rather than taking
    it as offered. Kept separate from ``proposal`` so the audit trail shows
    both what the agent asked for and what the human allowed."""

    def is_expired_at(self, now: datetime) -> bool:
        return self.status is ApprovalStatus.PENDING and now >= self.expires_at

    def effective_proposal(self) -> dict[str, Any]:
        """What should actually be performed, if anything."""
        return self.modified_proposal or self.proposal

    def to_review_payload(self) -> dict[str, Any]:
        """What a reviewer, or the console's queue, is given.

        The fields the decision was made on are lifted to the top level rather
        than left nested inside ``decision``, because a queue row that hides
        the risk tier one level down is a queue row people stop reading.
        """
        return {
            **self.to_payload(),
            "proposed_action": self.proposal,
            "risk_tier": self.decision.get("risk_tier"),
            "rule_id": self.decision.get("rule_id"),
            "required_scope": self.decision.get("required_scope"),
            "reasons": self.decision.get("reasons") or [],
        }

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = str(self.status)
        for key in ("created_at", "expires_at", "decided_at"):
            value = payload.get(key)
            payload[key] = value.isoformat() if isinstance(value, datetime) else None
        return payload


def fingerprint(proposal: dict[str, Any]) -> str:
    """A stable identity for "this exact proposal".

    Canonical JSON then SHA-256, the same construction the tool layer uses for
    idempotency keys, and for the same reason: two spellings of one request
    must not become two requests. Here it means a node re-executed after an
    interrupt recognises the row it already wrote.
    """
    canonical = json.dumps(proposal, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def build_request(
    *,
    run_id: str,
    ticket_id: str,
    thread_id: str,
    tenant_id: str,
    merchant_id: str,
    decision: dict[str, Any],
    proposal: dict[str, Any],
    expires_after_hours: int,
    now: datetime | None = None,
) -> ApprovalRequest:
    """Construct a pending request with a derived, stable id.

    The id is derived from the run and the proposal rather than generated,
    which is what makes the upsert on node re-execution land on the same row.
    """
    now = now or datetime.now(UTC)
    proposal_print = fingerprint(proposal)
    return ApprovalRequest(
        approval_id=f"apr_{fingerprint({'run': run_id, 'f': proposal_print})[:20]}",
        run_id=run_id,
        ticket_id=ticket_id,
        thread_id=thread_id,
        tenant_id=tenant_id,
        merchant_id=merchant_id,
        fingerprint=proposal_print,
        decision=decision,
        proposal=proposal,
        created_at=now,
        expires_at=now + timedelta(hours=expires_after_hours),
    )


@runtime_checkable
class ApprovalStore(Protocol):
    """Where pending approvals live.

    A protocol because the in-memory implementation is what the tests use and
    the Postgres one is what a deployment uses, and neither should be the
    other's special case.
    """

    def upsert(self, request: ApprovalRequest) -> ApprovalRequest: ...

    def get(self, approval_id: str) -> ApprovalRequest | None: ...

    def find_for_proposal(self, *, run_id: str, fingerprint: str) -> ApprovalRequest | None: ...

    def pending(self, *, tenant_id: str | None = None) -> list[ApprovalRequest]: ...

    def decide(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        reviewer: str,
        note: str | None = None,
        modified_proposal: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> ApprovalRequest: ...

    def sweep_expired(self, *, now: datetime | None = None) -> list[ApprovalRequest]: ...


@dataclass
class InMemoryApprovalStore:
    """The reference implementation, and the one the tests run against."""

    _rows: dict[str, ApprovalRequest] = field(default_factory=dict)

    def upsert(self, request: ApprovalRequest) -> ApprovalRequest:
        existing = self._rows.get(request.approval_id)
        if existing is not None:
            # A node re-executed after its interrupt is not a new request, and
            # must not reopen one a reviewer has already answered.
            return existing
        self._rows[request.approval_id] = request
        return request

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self._rows.get(approval_id)

    def find_for_proposal(self, *, run_id: str, fingerprint: str) -> ApprovalRequest | None:
        """The row for this exact proposal, whatever state it is now in.

        Whatever state: a node resuming after an interrupt needs the decided
        row, so a lookup that filtered to pending would miss the answer it
        came back for.
        """
        for row in self._rows.values():
            if row.run_id == run_id and row.fingerprint == fingerprint:
                return row
        return None

    def pending(self, *, tenant_id: str | None = None) -> list[ApprovalRequest]:
        rows = [r for r in self._rows.values() if r.status is ApprovalStatus.PENDING]
        if tenant_id is not None:
            rows = [r for r in rows if r.tenant_id == tenant_id]
        # Oldest first: a queue a reviewer works top-down should not reorder
        # under them as rows arrive.
        return sorted(rows, key=lambda r: (r.created_at, r.approval_id))

    def decide(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        reviewer: str,
        note: str | None = None,
        modified_proposal: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> ApprovalRequest:
        row = self._rows.get(approval_id)
        if row is None:
            raise ApprovalError(f"no approval request {approval_id!r}")
        if row.status.is_terminal:
            raise ApprovalError(
                f"approval {approval_id} is already {row.status}; "
                "a recorded decision is not overwritten"
            )
        if not status.is_terminal:
            raise ApprovalError("a decision must move the request out of pending")
        if status is ApprovalStatus.MODIFIED and modified_proposal is None:
            raise ApprovalError("a modified approval must carry the modified proposal")

        decided = replace(
            row,
            status=status,
            reviewer=reviewer,
            reviewer_note=note,
            modified_proposal=modified_proposal,
            decided_at=now or datetime.now(UTC),
        )
        self._rows[approval_id] = decided
        return decided

    def sweep_expired(self, *, now: datetime | None = None) -> list[ApprovalRequest]:
        """Expire pending requests past their deadline.

        Returns what it changed so the caller can act on each one — expiring
        the row is bookkeeping; the merchant still has to be told, and that is
        the sweep caller's job, not the store's.
        """
        now = now or datetime.now(UTC)
        expired: list[ApprovalRequest] = []
        for approval_id, row in list(self._rows.items()):
            if row.is_expired_at(now):
                moved = replace(
                    row,
                    status=ApprovalStatus.EXPIRED,
                    decided_at=now,
                    reviewer=None,
                    reviewer_note="expired without a reviewer decision",
                )
                self._rows[approval_id] = moved
                expired.append(moved)
        return expired

    # -- test and console helpers ---------------------------------------

    def reschedule(self, approval_id: str, expires_at: datetime) -> ApprovalRequest:
        """Move a request's deadline.

        For tests and for the eval harness, which has to reach S04's expired
        branch without waiting 48 hours. Moving the deadline rather than
        faking the clock keeps the sweep under test reading the same field it
        reads in production.
        """
        row = self._rows.get(approval_id)
        if row is None:
            raise ApprovalError(f"no approval request {approval_id!r}")
        moved = replace(row, expires_at=expires_at)
        self._rows[approval_id] = moved
        return moved

    def all(self) -> Iterable[ApprovalRequest]:
        return list(self._rows.values())

    def clear(self) -> None:
        self._rows.clear()
