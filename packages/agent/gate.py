"""Proposal, decision, approval, action.

Four nodes, in that order, and the order is the point. The agent proposes
using nothing but facts already read from the systems of record; the policy
engine decides; a human is asked only when the engine says a human is needed;
and exactly one node moves money.

The invariant that shapes all of this is from ADR 0001:

    A node that interrupts performs no mutation before the interrupt.

It sounds like a style rule and it is not. LangGraph implements ``interrupt()``
by raising, discarding the rest of the node, and re-executing that node from
its first line when the run resumes. So anything a node does before its
interrupt happens at least twice, and a refund issued before an interrupt is a
refund issued twice — once on the way to asking permission, once on the way
back. Splitting ``await_approval`` from ``act`` is what makes that structurally
impossible rather than carefully avoided: ``act`` has no interrupt in it, and
``await_approval`` touches nothing but the approval index, which is idempotent
precisely because it is on the re-executed path.

The second thing the split buys is honesty about what a reviewer is answering.
The interrupt payload is the decision as it was evaluated — rule id, inputs,
amount, citations — and not a model's summary of it. A reviewer approving a
paraphrase has approved nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langgraph.types import interrupt

from packages.agent.context import NodeContext
from packages.agent.state import AgentState
from packages.policy import approval as approvals
from packages.policy.approval import (
    PROCEEDING,
    ApprovalStatus,
    build_request,
    fingerprint,
)
from packages.policy.engine import Decision, Permission, ProposedAction, evaluate
from packages.shared.ledger import RunStep, StepKind, StepOutcome
from packages.shared.money import Money
from packages.tools.errors import ToolError
from packages.tools.reconcile import (
    Reconciliation,
    Resolution,
    needs_reconciliation,
    reconcile_refund,
)

__all__ = ["act", "await_approval", "decide", "propose"]


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------


#: Intents whose answer is an action on the account rather than a refund.
#: Each is proposed so the policy engine *classifies and refuses* it, which is
#: not a formality: S08's reply has to say the system cannot lift a payout hold
#: and name the queue that can, and a reply that says so because the drafter
#: improvised it is indistinguishable, in the text, from one grounded in a
#: rule. Proposing the action produces a decision with a tier and a rule id
#: behind the sentence. The action is never attempted — ``_after_decision``
#: routes `forbidden` to the drafter, never to ``act``.
PROPOSAL_FOR_INTENT: dict[str, ProposedAction] = {
    "payout_hold_query": ProposedAction(kind="lift_hold", touches=("payout_hold",)),
    # A chargeback notice is not a refund request, whatever the merchant
    # writes. The action they are implicitly asking for is to accept the
    # chargeback, which this deployment forbids — so it is proposed, refused,
    # and the ticket goes to the disputes queue with the evidence packet
    # attached. Refunding instead would pay the buyer twice.
    "dispute_notice": ProposedAction(kind="accept_chargeback", touches=("chargeback",)),
}


async def propose(state: AgentState, context: NodeContext) -> dict[str, Any]:
    """Turn established facts into a concrete proposal.

    No model call. The amount comes from the capture's unrefunded remainder or
    from what the merchant asked for, whichever is smaller, and the reason code
    comes from the triaged intent through a fixed mapping. A model asked to
    produce an amount would produce a plausible one, and plausible is the
    failure mode that costs money (ADR 0004).
    """
    intent = str((state.get("triage") or {}).get("intent", ""))
    fixed = PROPOSAL_FOR_INTENT.get(intent)
    if fixed is not None:
        return {"proposal": fixed.to_payload(), "visited": ["propose"]}

    capture = _target_capture(state)
    if capture is None:
        return {
            "proposal": None,
            "status": "completed",
            "visited": ["propose"],
            "proposal_note": (
                "no refundable capture was established, so there is nothing to propose"
            ),
        }

    remaining = _remainder(capture)
    requested = _requested_amount(state, default=remaining)
    amount = min(requested, remaining, key=lambda m: m.amount_minor)

    # One capture, because a refund targets a capture — the domain model is
    # explicit that there is no endpoint for refunding a payment. Listing
    # every refundable capture here was wrong twice over: it described a
    # refund that touches captures it does not touch, and it tripped the
    # tenant's multi-capture approval rule on a single-capture refund, so
    # S02's duplicate charge went to a reviewer instead of through.
    captures_touched = (str(capture["capture_id"]),)

    action = ProposedAction(
        kind="create_refund",
        capture_id=str(capture["capture_id"]),
        amount=amount,
        reason_code=_reason_code(state),
        captures_touched=captures_touched,
    )
    return {
        "proposal": action.to_payload(),
        "target_capture": capture,
        "visited": ["propose"],
    }


# The triaged intent decides the reason code, through a table rather than a
# prompt. `fraud` is deliberately absent: the agent never labels a transaction
# fraudulent, because that label has consequences past the refund and the
# tenant excludes it from the autonomous set for exactly that reason.
REASON_FOR_INTENT = {
    "refund_request": "buyer_request",
    "complaint_no_action": "goodwill",
    "dispute_notice": "buyer_request",
}
DEFAULT_REASON = "buyer_request"


def _reason_code(state: AgentState) -> str:
    intent = str((state.get("triage") or {}).get("intent", ""))
    if (state.get("duplicate_captures") or []) and intent == "refund_request":
        # Two captures against one order reference is the duplicate-charge
        # shape (S02), and `duplicate` is in the tenant's autonomous set where
        # `buyer_request` for the same money might not be. The evidence, not
        # the merchant's wording, picks the code.
        return "duplicate"
    return REASON_FOR_INTENT.get(intent, DEFAULT_REASON)


def _target_capture(state: AgentState) -> dict[str, Any] | None:
    """Which capture to refund.

    A duplicate, when the specialist found one. Two captures sharing a
    gateway reference are a retry artefact, and the later is the duplicate —
    so refunding the *first* refundable capture refunds the charge the buyer
    intended and leaves the accidental one standing. The money moved is the
    same and the record is wrong, which is worse than an error: it reconciles.
    """
    captures = state.get("captures") or []
    duplicates = set(state.get("duplicate_captures") or ())

    for capture in captures:
        if str(capture.get("capture_id")) in duplicates and _is_refundable(capture):
            return capture
    for capture in captures:
        if _is_refundable(capture):
            return capture
    return None


def _is_refundable(capture: dict[str, Any]) -> bool:
    try:
        return _remainder(capture).amount_minor > 0
    except (KeyError, ValueError):
        return False


def _remainder(capture: dict[str, Any]) -> Money:
    return Money.from_payload(capture["amount"]) - Money.from_payload(capture["refunded_amount"])


def _requested_amount(state: AgentState, *, default: Money) -> Money:
    """What the merchant asked for, if the ticket named a figure.

    An amount extracted from untrusted text is a *ceiling*, never an
    authority: it can only reduce what the remainder already allows. That is
    ADR 0005 rule 3 — the ticket is re-validated against the ledger, and the
    ledger wins.
    """
    stated = (state.get("resolution") or {}).get("stated_amount")
    if not stated:
        return default
    try:
        return Money.from_payload(stated)
    except (KeyError, TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------


async def decide(state: AgentState, context: NodeContext) -> dict[str, Any]:
    """Run the policy engine. The only node that produces a Decision."""
    proposal = state.get("proposal")
    if not proposal:
        return {"visited": ["decide"]}

    action = ProposedAction.from_payload(proposal)
    decision = evaluate(
        action,
        tenant=context.tenant,
        merchant=state.get("merchant") or {"tier": "standard"},
        capture=state.get("target_capture"),
        now=datetime.now(UTC),
    )
    # The decision goes in the ledger, not only into state. S05 asks for a
    # recorded policy-decision step carrying the rule id and the inputs it
    # was evaluated against, and the reason is the same one the rule ids
    # exist for: a decision a reviewer cannot find afterwards is a decision
    # they have to take on trust.
    _record_decision(context, state, decision)

    return {
        "decision": decision.to_payload(),
        "status": (
            "awaiting_approval"
            if decision.permission is Permission.NEEDS_APPROVAL
            else state.get("status", "running")
        ),
        "visited": ["decide"],
    }


#: How a permission lands in the ledger. A refusal is not an error — the
#: engine worked — so `refused` and `forbidden` are recorded as refusals
#: rather than as failures, and a trace is not red where the system behaved.
OUTCOME_FOR_PERMISSION = {
    Permission.PERMITTED: StepOutcome.SUCCESS,
    Permission.NEEDS_APPROVAL: StepOutcome.SUCCESS,
    Permission.FORBIDDEN: StepOutcome.REFUSED,
    Permission.REFUSED: StepOutcome.REFUSED,
}


def _record_decision(context: NodeContext, state: AgentState, decision: Decision) -> None:
    ledger = context.invoker.ledger
    run_id = str(state["run_id"])
    now = datetime.now(UTC)
    ledger.append(
        RunStep(
            run_id=run_id,
            seq=ledger.next_seq(run_id),
            node="decide",
            kind=StepKind.POLICY_DECISION,
            outcome=OUTCOME_FOR_PERMISSION[decision.permission],
            started_at=now,
            ended_at=now,
            policy_rule_id=decision.rule_id,
            detail=str(decision.permission),
        ),
        # The inputs are a payload rather than columns: which facts a decision
        # turned on depends on which rule fired, and a schema that pinned them
        # would need changing every time the engine records one more.
        payloads={"input": decision.inputs, "output": decision.to_payload()},
    )


def _record_approval(
    context: NodeContext, state: AgentState, request: approvals.ApprovalRequest
) -> None:
    ledger = context.invoker.ledger
    run_id = str(state["run_id"])
    now = datetime.now(UTC)
    ledger.append(
        RunStep(
            run_id=run_id,
            seq=ledger.next_seq(run_id),
            node="await_approval",
            kind=StepKind.APPROVAL,
            outcome=(StepOutcome.SUCCESS if request.status in PROCEEDING else StepOutcome.REFUSED),
            started_at=now,
            ended_at=now,
            policy_rule_id=request.decision.get("rule_id"),
            detail=f"{request.status} by {request.reviewer or 'nobody'}",
        ),
        payloads={"output": request.to_review_payload()},
    )


# ---------------------------------------------------------------------------
# await_approval
# ---------------------------------------------------------------------------


async def await_approval(state: AgentState, context: NodeContext) -> dict[str, Any]:
    """Pause for a human, durably.

    Everything above the ``interrupt()`` call runs again when the run resumes,
    so everything above it is either a read or an idempotent upsert. There is
    no mutation of a system of record anywhere in this function — that is the
    ADR 0001 invariant, and it is kept by this node having nothing to mutate
    rather than by remembering not to.
    """
    proposal = state.get("proposal") or {}
    decision = state.get("decision") or {}
    run_id = str(state["run_id"])
    proposal_print = fingerprint(proposal)

    # On re-execution after the resume, the row already exists and carries the
    # reviewer's answer. Reading it first is what lets this node tell "nobody
    # has been asked yet" from "the answer is already in".
    evidence = _evidence_for_reviewer(state)
    existing = context.approvals.find_for_proposal(run_id=run_id, fingerprint=proposal_print)
    request = existing or context.approvals.upsert(
        build_request(
            run_id=run_id,
            ticket_id=str(state["ticket_id"]),
            thread_id=f"ticket:{state['ticket_id']}",
            tenant_id=context.tenant.tenant_id,
            merchant_id=str(state.get("merchant_id", "")),
            decision=decision,
            proposal=proposal,
            evidence=evidence,
            expires_after_hours=int(context.tenant.approvals.get("expires_after_hours", 48)),
        )
    )

    if request.status is ApprovalStatus.PENDING:
        # Blocks here. The payload is what a reviewer sees, and it is the
        # decision as evaluated rather than a description of it.
        resumed = interrupt(
            {
                "kind": "approval_required",
                "approval_id": request.approval_id,
                "expires_at": request.expires_at.isoformat(),
                "decision": decision,
                "proposal": proposal,
                "evidence": evidence,
            }
        )
        # The resume value is advisory. The store is authoritative, because an
        # approval can also arrive through the API, and a resume payload is
        # the one thing here an attacker-shaped input could reach.
        request = (
            context.approvals.get(str(resumed.get("approval_id", request.approval_id)))
            if isinstance(resumed, dict)
            else context.approvals.get(request.approval_id)
        ) or request

    _record_approval(context, state, request)
    return _approval_outcome(request)


def _approval_outcome(request: approvals.ApprovalRequest) -> dict[str, Any]:
    if request.status in PROCEEDING:
        return {
            "approval": request.to_payload(),
            "proposal": request.effective_proposal(),
            "status": "running",
            "visited": ["await_approval"],
        }

    # Rejected or expired. Both end the run without a mutation, and the
    # distinction is kept because they are different things to tell a
    # merchant: one is a decision, the other is a queue that failed them.
    return {
        "approval": request.to_payload(),
        "status": "completed" if request.status is ApprovalStatus.REJECTED else "failed",
        "failure": None if request.status is ApprovalStatus.REJECTED else "approval_expired",
        "visited": ["await_approval"],
    }


def _evidence_for_reviewer(state: AgentState) -> dict[str, Any]:
    """The facts behind the proposal, for the console's evidence panel.

    Ids and figures read from the systems of record, plus the passages the
    reply rests on. Deliberately not the ticket body: a reviewer deciding a
    refund should be looking at the ledger, and putting untrusted text in the
    panel that authorises money is how an injected instruction gets a human to
    carry it (ADR 0005, rule 4).
    """
    return {
        "payment_id": (state.get("payment") or {}).get("payment_id"),
        "captures": [
            {
                "capture_id": c.get("capture_id"),
                "amount": c.get("amount"),
                "refunded_amount": c.get("refunded_amount"),
                "captured_at": c.get("captured_at"),
            }
            for c in state.get("captures") or []
        ],
        "duplicate_captures": state.get("duplicate_captures") or [],
        "resolution": state.get("resolution") or {},
        "citations": [p.get("passage_id") for p in state.get("passages") or []],
    }


# ---------------------------------------------------------------------------
# act
# ---------------------------------------------------------------------------


async def act(state: AgentState, context: NodeContext) -> dict[str, Any]:
    """Perform the refund. The only node in the graph that moves money.

    No interrupt, by construction: a node that both mutates and interrupts is
    a node that mutates twice.
    """
    proposal = state.get("proposal") or {}
    decision = state.get("decision") or {}
    approval = state.get("approval") or {}

    permission = decision.get("permission")
    if permission == str(Permission.PERMITTED):
        initiated_by = "agent"
    elif approval.get("status") in {str(s) for s in PROCEEDING}:
        initiated_by = "human"
    else:
        # Refusing here is belt and braces — the graph's edges should never
        # route an unapproved proposal to this node. It stays because the cost
        # of the check is nothing and the cost of the edge being wrong once is
        # a refund nobody authorised.
        return {
            "status": "failed",
            "failure": f"act reached without authority: permission={permission!r}",
            "visited": ["act"],
        }

    arguments = {
        "capture_id": proposal["capture_id"],
        "amount": proposal["amount"],
        "reason_code": proposal["reason_code"],
        "initiated_by": initiated_by,
        "approval_id": approval.get("approval_id"),
    }
    run_id = str(state["run_id"])
    try:
        result = await context.invoker.call("create_refund", arguments, run_id=run_id, node="act")
    except ToolError as error:
        return await _after_a_failed_refund(
            context, state, error=error, arguments=arguments, run_id=run_id
        )

    return {
        "refund": result.data,
        # `replayed` says the system of record returned a stored response
        # rather than creating a second refund. Carried into state because a
        # reply that says "I've issued your refund" when the refund already
        # existed is a different sentence from one that issued it.
        "refund_replayed": result.replayed,
        "status": "running",
        "visited": ["act"],
    }


async def _after_a_failed_refund(
    context: NodeContext,
    state: AgentState,
    *,
    error: ToolError,
    arguments: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    """The invoker has exhausted its attempts. Did the money move?

    Nothing in this run can answer that from memory: a timeout after the
    request was accepted looks exactly like a timeout before it was received.
    So the question goes to the system of record rather than to a guess.

    All three answers are terminal here. Even ABSENT — the refund demonstrably
    did not happen — does not retry, because the invoker already spent its
    attempts and a fresh one from this node would be a second retry budget
    nobody granted.
    """
    if not needs_reconciliation(error.failure, mutating=True):
        # A FATAL is the dependency telling us something true; a
        # POISONED_INPUT never reached the network. Neither leaves anything
        # upstream to find, so there is nothing to reconcile and the failure
        # stands as reported.
        raise error

    found = await reconcile_refund(
        context.invoker,
        run_id=run_id,
        node="act",
        arguments=arguments,
        capture_id=str(arguments["capture_id"]),
    )
    _record_reconciliation(context, state, found)

    if found.resolution is Resolution.LANDED:
        # The refund exists upstream. The call failed and the effect
        # happened, which is the case a naive implementation reports as a
        # failure while the merchant's money is already on its way.
        return {
            "refund": found.effect,
            "refund_replayed": True,
            "reconciliation": found.to_payload(),
            "status": "running",
            "visited": ["act"],
        }

    if found.resolution is Resolution.ABSENT:
        # Read successfully, nothing there. No money moved, so this is a
        # clean failure: the merchant can be told plainly and the run can be
        # replayed later without risking a duplicate.
        return {
            "reconciliation": found.to_payload(),
            "status": "failed",
            "failure": "refund_failed_without_effect",
            "visited": ["act"],
        }

    # UNKNOWN. The read itself failed, so the question is still open, and
    # the one thing that must not happen is another attempt. This is what
    # the dead-letter queue is for: a human reads the ledger, checks the
    # gateway, and decides.
    return {
        "reconciliation": found.to_payload(),
        "status": "failed",
        "failure": "refund_indeterminate_needs_human",
        "visited": ["act"],
    }


def _record_reconciliation(context: NodeContext, state: AgentState, found: Reconciliation) -> None:
    ledger = context.invoker.ledger
    run_id = str(state["run_id"])
    now = datetime.now(UTC)
    ledger.append(
        RunStep(
            run_id=run_id,
            seq=ledger.next_seq(run_id),
            node="act",
            kind=StepKind.COMPENSATION,
            outcome=(StepOutcome.SUCCESS if found.is_settled else StepOutcome.ERROR),
            started_at=now,
            ended_at=now,
            tool="create_refund",
            error_code=None if found.is_settled else "reconciliation_unknown",
            detail=f"{found.resolution}: {found.detail}",
        ),
        payloads={"output": found.to_payload()},
    )
