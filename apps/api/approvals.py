"""The approval endpoints' logic, and the expiry sweep.

Separate from ``main.py`` so the rules a reviewer's decision has to satisfy —
the note a rejection requires, the amount a modification may not exceed, the
terminal state a second reviewer cannot overwrite — are readable in one place
rather than spread through route handlers.

Two things here are load-bearing and easy to get wrong.

**Recording the decision and resuming the run are separate steps, in that
order.** The store is written first, then the graph is resumed. If the resume
fails — the process dies, the checkpointer is unreachable — the decision
survives and the run can be resumed again, because ``await_approval`` reads
the store rather than trusting the resume payload. Resuming first and
recording after would lose the reviewer's answer on exactly the failure the
durable checkpoint exists to survive.

**The sweep is not optional.** LangGraph will hold a thread paused forever;
nothing in it expires. A pending approval nobody answers has to be expired
from outside, and the run resumed so it terminates and the merchant is told.
Without the sweep the failure mode is silence, which is the worst available
outcome: the merchant waits, the queue looks healthy, and nothing is wrong
anywhere a dashboard would show it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, ConfigDict, Field

from packages.policy.approval import (
    ApprovalError,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalStore,
)
from packages.policy.tenant import TenantConfig
from packages.shared.money import Money
from packages.shared.problem import (
    STATE_CONFLICT,
    VALIDATION_FAILED,
    ProblemError,
    not_found,
)

#: What a reviewer may answer, mapped to the stored status. ``expired`` is
#: absent deliberately: it is something time does, not something a human
#: chooses, and offering it as a decision would let a reviewer disguise a
#: rejection as a timeout.
DECISIONS: dict[str, ApprovalStatus] = {
    "approve": ApprovalStatus.APPROVED,
    "reject": ApprovalStatus.REJECTED,
    "modify": ApprovalStatus.MODIFIED,
}


class ReviewerDecision(BaseModel):
    """A reviewer's answer, as the console sends it."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject", "modify"]
    reviewer: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    amount_minor: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Required for `modify`, and in minor units: a reviewer's edit goes "
            "through the same integer representation as everything else that "
            "touches money."
        ),
    )


def validate_decision(
    decision: ReviewerDecision,
    request: ApprovalRequest,
    *,
    tenant: TenantConfig,
) -> dict[str, Any] | None:
    """Check the answer against the tenant's rules; return the modified proposal.

    Returns ``None`` for approve and reject, which carry no proposal change.
    Raises ``ProblemError`` rather than returning a flag, so a rejected
    decision reaches the console as the same RFC 9457 shape as every other
    refusal in this API.
    """
    status = DECISIONS[decision.action]

    require_note_on = set(tenant.approvals.get("require_reviewer_note_on") or ())
    if str(status) in require_note_on and not (decision.note or "").strip():
        raise ProblemError(
            VALIDATION_FAILED,
            f"this tenant requires a reviewer note on {decision.action}",
        )

    if status is not ApprovalStatus.MODIFIED:
        if decision.amount_minor is not None:
            raise ProblemError(
                VALIDATION_FAILED,
                f"an amount may only accompany `modify`, not `{decision.action}`",
            )
        return None

    if decision.amount_minor is None:
        raise ProblemError(VALIDATION_FAILED, "`modify` requires amount_minor")

    proposed = request.proposal.get("amount") or {}
    try:
        original = Money.from_payload(proposed)
    except (KeyError, TypeError, ValueError) as error:
        raise ProblemError(
            VALIDATION_FAILED, f"the stored proposal has no readable amount: {error}"
        ) from error

    # A reviewer may reduce what the agent proposed, never increase it. The
    # proposal was evaluated against the capture's unrefunded remainder and
    # the tenant's window; a larger figure has been checked against nothing,
    # and arrives at `act` carrying an approval that appears to authorise it.
    if decision.amount_minor > original.amount_minor:
        raise ProblemError(
            VALIDATION_FAILED,
            (
                f"a reviewer may reduce the proposed {original.format()} but not raise it; "
                f"{Money(decision.amount_minor, original.currency).format()} is higher. "
                "Refusing the proposal and letting the agent re-evaluate is the way to "
                "refund more."
            ),
        )

    return {
        **request.proposal,
        "amount": Money(decision.amount_minor, original.currency).to_payload(),
    }


def record(
    store: ApprovalStore,
    approval_id: str,
    decision: ReviewerDecision,
    *,
    tenant: TenantConfig,
) -> ApprovalRequest:
    """Validate and persist a reviewer's answer.

    Persisting before the resume is the ordering that makes a crashed resume
    recoverable rather than a lost decision.
    """
    request = store.get(approval_id)
    if request is None:
        raise not_found("approval request", approval_id)
    if request.status.is_terminal:
        # 409 and not 400: the request is well formed, and the conflict is
        # with the state of the world. A console retrying a 400 would be
        # retrying its own mistake; on a 409 it knows to reload the row.
        raise ProblemError(
            STATE_CONFLICT,
            f"approval {approval_id} was already {request.status}"
            + (f" by {request.reviewer}" if request.reviewer else ""),
        )

    modified = validate_decision(decision, request, tenant=tenant)
    try:
        return store.decide(
            approval_id,
            status=DECISIONS[decision.action],
            reviewer=decision.reviewer,
            note=decision.note,
            modified_proposal=modified,
            now=datetime.now(UTC),
        )
    except ApprovalError as error:
        # Lost the race against another reviewer or the sweep between the
        # check above and this write. Whoever got there first wins.
        raise ProblemError(STATE_CONFLICT, str(error)) from error


def queue_payload(store: ApprovalStore, *, tenant_id: str) -> dict[str, Any]:
    """The reviewer console's queue."""
    rows = store.pending(tenant_id=tenant_id)
    now = datetime.now(UTC)
    return {
        "count": len(rows),
        "approvals": [
            {
                **row.to_review_payload(),
                # Surfaced because a queue sorted by age tells a reviewer
                # which rows are about to expire out from under them.
                "expires_in_seconds": max(0, int((row.expires_at - now).total_seconds())),
            }
            for row in rows
        ],
    }


if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from apps.api.main import Dependencies
    from apps.api.runs import Run


def build_router(
    *,
    get_deps: Callable[[Request], Dependencies],
    resume: Callable[..., Coroutine[Any, Any, None]],
    sweep: Callable[[Dependencies], Coroutine[Any, Any, dict[str, Any]]],
    background: set[asyncio.Task[None]],
) -> APIRouter:
    """The reviewer console's endpoints.

    Built by a factory taking its collaborators as arguments rather than
    importing them: the resume path lives in ``main`` and ``main`` mounts this
    router, so an import either way round is a cycle. Passing them in also
    means a test can mount this router with a stub resume and assert the
    decision was recorded without running a graph.
    """
    router = APIRouter(prefix="/approvals", tags=["approvals"])

    @router.get("")
    async def list_approvals(request: Request) -> dict[str, Any]:
        deps = get_deps(request)
        return queue_payload(deps.approvals, tenant_id=deps.tenant.tenant_id)

    @router.get("/{approval_id}")
    async def get_approval(approval_id: str, request: Request) -> dict[str, Any]:
        deps = get_deps(request)
        stored = deps.approvals.get(approval_id)
        if stored is None:
            raise not_found("approval request", approval_id)
        return stored.to_review_payload()

    @router.post("/{approval_id}/decision")
    async def decide_approval(
        approval_id: str,
        request: Request,
        decision: Annotated[ReviewerDecision, Body()],
    ) -> dict[str, Any]:
        """Record a reviewer's answer, then resume the run that was waiting.

        In that order. If the resume fails the decision survives and the run
        is resumable, because ``await_approval`` reads the store rather than
        trusting the resume payload.
        """
        deps = get_deps(request)
        recorded = record(deps.approvals, approval_id, decision, tenant=deps.tenant)

        run: Run | None = deps.store.get(recorded.run_id)
        if run is None:
            # The decision stands; there is simply no live run to continue,
            # which happens when the API restarted without a persistent run
            # store. Reported rather than swallowed.
            return {
                **recorded.to_review_payload(),
                "resumed": False,
                "detail": f"no run {recorded.run_id} in this process to resume",
            }

        continuation = resume(run, deps, approval_id=approval_id)
        task: asyncio.Task[None] = asyncio.create_task(continuation)
        background.add(task)
        task.add_done_callback(background.discard)
        return {**recorded.to_review_payload(), "resumed": True}

    @router.post("/sweep")
    async def sweep_approvals(request: Request) -> dict[str, Any]:
        """Expire overdue approvals and let their runs terminate.

        Exposed as an endpoint rather than left to the background loop so the
        eval harness can drive S04's expired branch without waiting 48 hours,
        and so an operator can run it on demand.
        """
        return await sweep(get_deps(request))

    return router
