"""Recording the outcome on the ticket.

Until this node existed the graph produced a reply, put it in state, and
stopped. Nothing reached the ticketing system of record — no status, no
message, no assignment — which meant the merchant saw nothing and the next
human to open the ticket saw an untouched `open` ticket with no trace that an
agent had worked it. Ten of the twelve golden fixtures assert the resulting
ticket status; the eval harness's first finding was that none of them could
pass.

What gets written depends on how the run ended, and the mapping is a table
rather than a chain of conditionals because each row is a decision somebody
should be able to read:

| How the run ended            | What is written                          |
| ---------------------------- | ---------------------------------------- |
| answered, nothing to do      | status resolved, reply posted            |
| refund issued                | status resolved, reply posted            |
| waiting for a reviewer       | status awaiting_approval                 |
| waiting for the merchant     | status awaiting_customer, question posted|
| refused, or needs a human    | assigned to a queue, reply posted        |

Assignment is what escalates. The ticketing service moves an `open` ticket to
`escalated` when it is assigned, so the escalating paths need
`ticketing:assign:write` and not `ticketing:ticket:write` — which is exactly
the distinction S07 and S12 draw in their `no_mutating_tool_calls` exception
lists, and a good sign the fixtures were written against a real contract.

Priority is never set. The ticketing service derives it from merchant tier
precisely so a merchant cannot raise their own by asking, and a caller that
could set it would be a caller that could be talked into setting it.
"""

from __future__ import annotations

from typing import Any

from packages.agent.context import NodeContext
from packages.agent.state import AgentState
from packages.policy.engine import Permission
from packages.tools.errors import ToolError

#: Which specialist queue a decision belongs to, read from what the action
#: touches rather than from the merchant's words. A ticket about a chargeback
#: goes to disputes because the capture has a chargeback against it, not
#: because the merchant used the word.
QUEUE_FOR_TOUCH: dict[str, str] = {
    "chargeback": "disputes",
    "kyc": "compliance",
    "payout_hold": "compliance",
    "sanctions": "compliance",
    "settlement_schedule": "settlements",
}

#: And for intents that escalate without touching a sensitive area.
QUEUE_FOR_INTENT: dict[str, str] = {
    "settlement_query": "settlements",
    "dispute_notice": "disputes",
    "payout_hold_query": "compliance",
    # A merchant complaining with nothing technically wrong is the case an
    # agent is worst at and a person is best at. There is nothing to fix, so
    # "resolved" would close a ticket on a merchant who is still unhappy, and
    # the only honest action is to put a human in front of them.
    "complaint_no_action": "general",
}
DEFAULT_QUEUE = "general"


async def settle(state: AgentState, context: NodeContext) -> dict[str, Any]:
    """Write the run's outcome where the merchant and the next human see it.

    Every write is attempted independently and a failure to record the
    outcome does not fail the run: the refund, if there was one, has already
    happened, and reporting the whole run as failed because the ticket could
    not be updated would send an operator looking for money that moved
    correctly. The failures are recorded in the ledger and on the state
    instead.
    """
    ticket_id = str(state["ticket_id"])
    run_id = str(state["run_id"])
    reply = state.get("reply") or {}
    decision = state.get("decision") or {}
    problems: list[str] = []

    async def attempt(tool: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        try:
            result = await context.invoker.call(tool, arguments, run_id=run_id, node="settle")
        except ToolError as error:
            problems.append(f"{tool}: {error.failure.code}")
            return None
        return dict(result.data) if isinstance(result.data, dict) else None

    # A ticket whose body imitated system instructions is flagged whatever
    # else happens, because the flag is what makes the pattern visible across
    # tickets rather than only in this one's ledger.
    if (state.get("triage") or {}).get("suspected_injection"):
        await attempt("flag_ticket", {"ticket_id": ticket_id, "flag": "suspected_injection"})

    if state.get("repeat_contact"):
        await attempt("flag_ticket", {"ticket_id": ticket_id, "flag": "repeat_contact"})

    outcome = _outcome_of(state, decision)

    if outcome == "awaiting_approval":
        await attempt(
            "set_ticket_status",
            {
                "ticket_id": ticket_id,
                "status": "awaiting_approval",
                "reason": _approval_reason(decision),
                "run_id": run_id,
            },
        )
        return _result(outcome, problems)

    # The reply, before any status change. A resolved ticket with no message
    # on it is a ticket that tells the merchant nothing, and the order means
    # a failure to post leaves the ticket open rather than silently closed.
    #
    # A refusal goes to the internal note rather than to the merchant. The
    # verifier declined to send it, and posting it outbound anyway would undo
    # that decision — an ungrounded reply reaching a merchant is the thing
    # the grounding check exists to prevent. It is recorded so the human
    # picking the ticket up can see what the agent would have said and why it
    # stopped, and the escalation below is what gets the merchant an answer.
    if reply.get("text"):
        refusing = bool(reply.get("is_refusal"))
        await attempt(
            "post_message",
            {
                "ticket_id": ticket_id,
                "direction": "internal_note" if refusing else "outbound",
                "author": "concord-agent",
                "body": (
                    f"Not sent to the merchant: {reply.get('reason', 'ungrounded')}. "
                    f"Drafted reply was: {reply['text']}"
                    if refusing
                    else str(reply["text"])
                ),
                "citations": list(reply.get("citations") or []),
            },
        )

    if outcome == "escalated":
        # A reviewer's note is the reason this was declined, and the next
        # person to open the ticket is the one who needs it. Leaving it on
        # the approval record alone means the handoff arrives without the
        # one thing that explains it.
        approval = state.get("approval") or {}
        if approval.get("reviewer_note"):
            await attempt(
                "post_message",
                {
                    "ticket_id": ticket_id,
                    "direction": "internal_note",
                    "author": str(approval.get("reviewer") or "reviewer"),
                    "body": str(approval["reviewer_note"]),
                    "citations": [],
                },
            )

        # Assignment escalates an `open` ticket by itself, which is why the
        # scenarios that escalate from open need only `assign:write`. A
        # ticket already moved to awaiting_approval is not open, so the
        # service leaves its status alone and the status has to be set here —
        # otherwise a declined approval leaves the ticket sitting in
        # awaiting_approval with nobody waiting on it.
        if approval.get("status"):
            await attempt(
                "set_ticket_status",
                {
                    "ticket_id": ticket_id,
                    "status": "escalated",
                    "reason": _escalation_reason(state, decision),
                    "run_id": run_id,
                },
            )

        queue = _queue_for(state, decision)
        await attempt(
            "assign_ticket",
            {
                "ticket_id": ticket_id,
                "queue": queue,
                "reason": _escalation_reason(state, decision),
                # The facts the next human needs, so the assignment arrives
                # with its evidence rather than as a bare handoff.
                "context": _handoff_context(state),
            },
        )
        return _result(outcome, problems, queue=queue)

    if outcome == "awaiting_customer":
        await attempt(
            "set_ticket_status",
            {
                "ticket_id": ticket_id,
                "status": "awaiting_customer",
                "reason": "asked the merchant which payment they meant",
                "run_id": run_id,
            },
        )
        return _result(outcome, problems)

    await attempt(
        "set_ticket_status",
        {
            "ticket_id": ticket_id,
            "status": "resolved",
            "reason": _resolution_reason(state),
            "run_id": run_id,
        },
    )
    return _result(outcome, problems)


def _outcome_of(state: AgentState, decision: dict[str, Any]) -> str:
    """How this run ended, in the ticket's vocabulary."""
    if state.get("status") == "awaiting_approval":
        return "awaiting_approval"

    if state.get("status") == "awaiting_customer":
        # A specialist asked a question and said so. The ticket waits for the
        # merchant, not for us.
        return "awaiting_customer"

    resolution = (state.get("resolution") or {}).get("outcome")
    if resolution in {"ambiguous", "not_found"}:
        return "awaiting_customer"

    permission = decision.get("permission")
    if permission in {str(Permission.FORBIDDEN), str(Permission.REFUSED)}:
        # Refused by the rules or out of scope entirely. Either way a human
        # owns it now: a refused request is not a resolved one.
        return "escalated"

    if state.get("status") == "failed":
        return "escalated"

    approval = state.get("approval") or {}
    if approval.get("status") in {"rejected", "expired"}:
        # A reviewer said no, or nobody said anything in time. Either way a
        # person owns this now: calling it resolved would close a ticket on
        # which the merchant's request was declined or simply dropped, and
        # the two need telling apart in the handoff rather than in the status.
        return "escalated"

    if (state.get("reply") or {}).get("is_refusal"):
        # The verifier would not send the reply, so a person has to. Calling
        # this resolved would close a ticket the merchant never heard about.
        return "escalated"

    if (state.get("triage") or {}).get("intent") in QUEUE_FOR_INTENT and not state.get("refund"):
        # An enquiry whose answer is "a specialist has this" rather than an
        # action we can take.
        return "escalated"

    return "resolved"


def _queue_for(state: AgentState, decision: dict[str, Any]) -> str:
    for touch in (state.get("proposal") or {}).get("touches") or ():
        if touch in QUEUE_FOR_TOUCH:
            return QUEUE_FOR_TOUCH[touch]
    if state.get("chargebacks") or state.get("evidence_packet"):
        return QUEUE_FOR_TOUCH["chargeback"]
    intent = str((state.get("triage") or {}).get("intent", ""))
    return QUEUE_FOR_INTENT.get(intent, DEFAULT_QUEUE)


def _approval_reason(decision: dict[str, Any]) -> str:
    reasons = decision.get("reasons") or []
    rule = decision.get("rule_id") or "policy"
    return f"{rule}: {reasons[0]}" if reasons else f"{rule}: a reviewer has to decide"


def _escalation_reason(state: AgentState, decision: dict[str, Any]) -> str:
    if decision.get("reasons"):
        return f"{decision.get('rule_id')}: {decision['reasons'][0]}"
    if state.get("failure"):
        return f"the run could not complete: {state['failure']}"
    return "needs a specialist"


def _resolution_reason(state: AgentState) -> str:
    refund = state.get("refund") or {}
    if refund.get("refund_id"):
        return f"refund {refund['refund_id']} issued and the merchant told"
    return "answered from the knowledge corpus; no action was required"


def _handoff_context(state: AgentState) -> dict[str, Any]:
    """What the next human needs, from the systems of record only.

    Not the ticket body. A handoff note is read by someone deciding what to
    do, which makes it an instruction-shaped place for injected text to sit
    (ADR 0005, rule 4).
    """
    context: dict[str, Any] = {
        "run_id": state.get("run_id"),
        "intent": (state.get("triage") or {}).get("intent"),
    }
    if state.get("decision"):
        context["rule_id"] = state["decision"].get("rule_id")
        context["risk_tier"] = state["decision"].get("risk_tier")
    if state.get("payment"):
        context["payment_id"] = state["payment"].get("payment_id")
    if state.get("evidence_packet"):
        context["evidence_packet"] = state["evidence_packet"]
    if state.get("contact_history"):
        # S12 asserts the history reaches the assignment: a merchant on their
        # fourth contact should not have to explain the first three.
        context["contact_history"] = state["contact_history"]
    if state.get("incidents"):
        context["incidents"] = [i.get("incident_id") for i in state["incidents"]]
    return context


def _result(outcome: str, problems: list[str], *, queue: str | None = None) -> dict[str, Any]:
    settled: dict[str, Any] = {"outcome": outcome}
    if queue is not None:
        settled["queue"] = queue
    if problems:
        settled["problems"] = problems
    return {"settled": settled, "visited": ["settle"]}


async def mark_pending(state: AgentState, context: NodeContext) -> dict[str, Any]:
    """Record on the ticket that a human is needed, before the graph pauses.

    Its own node rather than part of `await_approval`, because of ADR 0001:
    everything above an `interrupt()` runs again on resume, so a node that
    interrupts must contain nothing that cannot bear repeating. This write
    can — setting a status to the value it already holds is a no-op in the
    ticketing service — but keeping it out of the interrupting node means the
    invariant is structural rather than argued, and the structural test that
    enforces it stays simple.

    The write is to the ticket, never to money. Nothing here moves anything a
    reviewer has not yet authorised.
    """
    decision = state.get("decision") or {}
    try:
        await context.invoker.call(
            "set_ticket_status",
            {
                "ticket_id": str(state["ticket_id"]),
                "status": "awaiting_approval",
                "reason": _approval_reason(decision),
                "run_id": str(state["run_id"]),
            },
            run_id=str(state["run_id"]),
            node="mark_pending",
        )
    except ToolError as error:
        # The approval still exists and the reviewer console still shows it.
        # A ticket that could not be updated is worth recording and not worth
        # abandoning the run over.
        return {
            "settled": {
                "outcome": "awaiting_approval",
                "problems": [f"set_ticket_status: {error.failure.code}"],
            },
            "visited": ["mark_pending"],
        }
    return {"settled": {"outcome": "awaiting_approval"}, "visited": ["mark_pending"]}
