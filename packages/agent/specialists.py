"""Specialist nodes, and the supervisor that routes to them.

The supervisor does not call a model. Intent was already classified by triage,
and routing is a lookup from intent to specialist — asking a model to decide
twice adds latency and a second chance to be wrong, with nothing gained.

Each specialist reads only what its intent needs. The transactions specialist
does not fetch settlements; the settlements specialist does not resolve a
payment. That is not tidiness: every tool call costs a round trip and widens
the set of facts a later node might use carelessly, and a specialist that
gathers everything is a specialist that cannot be reasoned about.

What they produce is facts, not conclusions. Whether a refund is permitted is
the policy engine's decision in phase 6; these nodes assemble the evidence it
will need and stop there (ADR 0004).
"""

from __future__ import annotations

from typing import Any

from packages.agent.context import NodeContext
from packages.agent.resolution import (
    Evidence,
    Outcome,
    clarifying_question,
    parse_amount,
    parse_last_four,
    resolve_payment,
)
from packages.agent.state import AgentState
from packages.shared.dates import format_day
from packages.shared.money import Money
from packages.tools.errors import ToolError

# Intent to specialist. Every intent the triage schema can produce appears
# here, so a new intent cannot silently fall through to a default.
# Two captures sharing a gateway reference is the retry signature.
DUPLICATE_GROUP_SIZE = 2
# At this many prior contacts a complaint is a repeat, which changes tone
# and priority but never what the agent is permitted to do.
REPEAT_CONTACT_THRESHOLD = 2

ROUTES: dict[str, str] = {
    "refund_request": "transactions",
    "status_question": "knowledge_only",
    "dispute_notice": "disputes",
    "settlement_query": "settlements",
    "payout_hold_query": "account",
    "complaint_no_action": "account",
    "unclear": "knowledge_only",
}


def route(state: AgentState) -> str:
    """Pick the specialist for this ticket's classified intent."""
    intent = (state.get("triage") or {}).get("intent", "unclear")
    return ROUTES.get(str(intent), "knowledge_only")


async def transactions(state: AgentState, context: NodeContext) -> dict[str, Any]:
    """Resolve which payment the ticket is about, and read its captures.

    The resolution runs over facts read from the payments ledger, not over
    what the ticket asserts. An order reference extracted from merchant text
    is a search key, and the record it finds is what everything downstream
    uses (ADR 0005, rule 3).
    """
    triage = state.get("triage") or {}
    body = state.get("body", "")

    evidence = Evidence(
        order_ref=triage.get("order_ref"),
        amount=parse_amount(body) if triage.get("mentions_amount") else None,
        buyer_last_four=parse_last_four(body),
    )

    arguments: dict[str, Any] = {"merchant_id": state["merchant_id"], "limit": 10}
    if evidence.order_ref:
        arguments["order_ref"] = evidence.order_ref

    found = await context.invoker.call(
        "search_payments", arguments, run_id=state["run_id"], node="transactions"
    )
    payments = found.data.get("items", [])

    resolution = resolve_payment(
        payments=payments, evidence=evidence, merchant_id=state["merchant_id"]
    )

    update: dict[str, Any] = {
        "resolution": resolution.to_payload(),
        "visited": ["transactions"],
    }

    if resolution.outcome is Outcome.AMBIGUOUS:
        # Terminating in a question is a successful outcome, not a failure.
        # A silent choice between two payments is what S11 exists to catch.
        update["reply"] = {
            "text": clarifying_question(resolution),
            "citations": [],
            "is_refusal": False,
            "is_question": True,
        }
        update["status"] = "awaiting_customer"
        return update

    if resolution.outcome is Outcome.NOT_FOUND:
        update["reply"] = {
            "text": (
                "I couldn't find a payment matching that reference on your account. "
                "Could you confirm the order reference, or the amount and date?"
            ),
            "citations": [],
            "is_refusal": False,
            "is_question": True,
        }
        update["status"] = "awaiting_customer"
        return update

    payment = resolution.payment or {}
    update["payment"] = payment
    update["captures"] = payment.get("captures", [])

    # A duplicate capture is a fact about the ledger, derived here so the
    # policy engine and the drafter both read one determination rather than
    # each deciding for themselves.
    update["duplicate_captures"] = _duplicate_captures(payment)
    return update


def _duplicate_captures(payment: dict[str, Any]) -> list[str]:
    """Captures that are retry artifacts rather than deliberate charges.

    Two captures on one payment sharing a gateway reference is the signature,
    per the domain model and the refund policy. The *later* one is the
    duplicate; the earlier is the charge the buyer intended.
    """
    by_reference: dict[str, list[dict[str, Any]]] = {}
    for capture in payment.get("captures", []):
        by_reference.setdefault(str(capture.get("gateway_reference")), []).append(capture)

    duplicates: list[str] = []
    for group in by_reference.values():
        if len(group) < DUPLICATE_GROUP_SIZE:
            continue
        ordered = sorted(group, key=lambda c: str(c.get("captured_at", "")))
        duplicates.extend(str(c["capture_id"]) for c in ordered[1:])
    return duplicates


async def settlements(state: AgentState, context: NodeContext) -> dict[str, Any]:
    """Read the merchant's settlements and any active platform incident.

    The incident lookup is a separate call rather than part of retrieval
    because an incident is a time-bounded fact, not a document: S07 may cite
    one only while it is active.
    """
    found = await context.invoker.call(
        "search_settlements",
        {"merchant_id": state["merchant_id"], "limit": 5},
        run_id=state["run_id"],
        node="settlements",
    )
    incidents = await context.invoker.call(
        "get_active_incidents",
        {"tenant_id": context.tenant_id, "affects": "settlements"},
        run_id=state["run_id"],
        node="settlements",
    )
    return {
        "settlements": found.data.get("items", []),
        "incidents": incidents.data.get("incidents", []),
        "visited": ["settlements"],
    }


async def disputes(state: AgentState, context: NodeContext) -> dict[str, Any]:
    """Assemble the evidence packet for an open dispute.

    Reads rather than acts. Accepting or contesting a chargeback is the
    merchant's decision and is a forbidden action for this agent, so this node
    gathers what a human needs and goes no further.
    """
    open_disputes = await context.invoker.call(
        "search_chargebacks",
        {"merchant_id": state["merchant_id"], "status": "open"},
        run_id=state["run_id"],
        node="disputes",
    )
    chargebacks = open_disputes.data.get("items", [])

    packet: dict[str, Any] = {"chargebacks": chargebacks}
    payment: dict[str, Any] | None = None

    if chargebacks:
        capture_id = chargebacks[0].get("capture_id")
        if capture_id:
            try:
                capture = await context.invoker.call(
                    "get_capture",
                    {"capture_id": capture_id},
                    run_id=state["run_id"],
                    node="disputes",
                )
                packet["capture"] = capture.data
                payment = await _payment_for(capture.data, state, context)
            except ToolError:
                # A missing capture is worth recording and not worth failing
                # the run over: the deadline still has to reach a human.
                packet["capture"] = None

    # The four things a human contesting a chargeback has to send the issuer,
    # named the way the dispute process names them. Assembling them here is
    # the whole value of this node: the deadline is days away and a human
    # should not spend one of them gathering ids.
    packet["authorization_trail"] = _authorization_trail(packet.get("capture"), payment)
    packet["delivery_reference"] = _delivery_reference(packet.get("capture"), payment)

    history = await context.invoker.call(
        "get_contact_history",
        {"merchant_id": state["merchant_id"], "limit": 10},
        run_id=state["run_id"],
        node="disputes",
    )
    packet["prior_contacts"] = history.data.get("items", [])

    # Every dispute this merchant has had, not only the open one. An issuer
    # weighs a first chargeback differently from a fifth, and a human
    # answering this needs to know which they are writing about.
    try:
        all_disputes = await context.invoker.call(
            "search_chargebacks",
            {"merchant_id": state["merchant_id"]},
            run_id=state["run_id"],
            node="disputes",
        )
        packet["prior_dispute_history"] = all_disputes.data.get("items", [])
    except ToolError:
        packet["prior_dispute_history"] = chargebacks

    return {
        "evidence_packet": packet,
        "chargebacks": chargebacks,
        "visited": ["disputes"],
    }


async def _payment_for(
    capture: dict[str, Any], state: AgentState, context: NodeContext
) -> dict[str, Any] | None:
    payment_id = capture.get("payment_id")
    if not payment_id:
        return None
    try:
        result = await context.invoker.call(
            "get_payment",
            {"payment_id": str(payment_id)},
            run_id=state["run_id"],
            node="disputes",
        )
    except ToolError:
        return None
    return dict(result.data)


def _authorization_trail(
    capture: dict[str, Any] | None, payment: dict[str, Any] | None
) -> dict[str, Any]:
    """When the card was authorised and under which gateway reference.

    The issuer's first question about a disputed card payment, and the
    answer is spread across the payment and the capture rather than sitting
    in one field.
    """
    capture = capture or {}
    payment = payment or {}
    return {
        "payment_id": payment.get("payment_id") or capture.get("payment_id"),
        "authorized_at": payment.get("authorized_at"),
        "captured_at": capture.get("captured_at"),
        "gateway_reference": capture.get("gateway_reference"),
        "method": payment.get("method"),
    }


def _delivery_reference(
    capture: dict[str, Any] | None, payment: dict[str, Any] | None
) -> dict[str, Any]:
    """What ties the payment to something the buyer received.

    The merchant's own order reference and the buyer reference the gateway
    holds. Neither proves delivery on its own, and saying so is more use to
    a human than a field called `delivery_reference` that implies it does.
    """
    payment = payment or {}
    return {
        "order_ref": payment.get("order_ref"),
        "buyer_reference": payment.get("buyer_reference"),
        "note": "identifiers only; proof of delivery is the merchant's to supply",
    }


async def account(state: AgentState, context: NodeContext) -> dict[str, Any]:
    """Read account standing and contact history.

    Used for payout holds and for complaints with nothing actionable. Contact
    history is what turns "the third time I'm writing" from an assertion into
    a verified fact.
    """
    history = await context.invoker.call(
        "get_contact_history",
        {"merchant_id": state["merchant_id"], "limit": 10},
        run_id=state["run_id"],
        node="account",
    )
    contacts = history.data.get("items", [])
    return {
        "contact_history": contacts,
        "repeat_contact": len(contacts) >= REPEAT_CONTACT_THRESHOLD,
        "visited": ["account"],
    }


async def knowledge_only(state: AgentState, context: NodeContext) -> dict[str, Any]:
    """No system-of-record lookup beyond the merchant record already read.

    A policy question needs the corpus, not the ledger. Reading a payment to
    answer "how long do refunds take" would be a tool call that costs a round
    trip and tells the agent nothing it uses.

    Takes the context it does not use so every specialist shares one
    signature, which is what lets the supervisor treat them uniformly.
    """
    del context
    return {"visited": ["knowledge_only"]}


SPECIALISTS = {
    "transactions": transactions,
    "settlements": settlements,
    "disputes": disputes,
    "account": account,
    "knowledge_only": knowledge_only,
}


def specialist_facts(state: AgentState) -> str:
    """The facts a specialist gathered, rendered for the drafting prompt.

    Assembled here rather than in the drafter so there is one place that
    decides what the model is told about the account — and so a new specialist
    cannot quietly widen it.
    """
    lines: list[str] = []

    payment = state.get("payment")
    if payment:
        amount = Money.from_payload(payment["amount"]).format()
        lines.append(f"payment {payment['payment_id']}: {amount}, status {payment['status']}")
        for capture in state.get("captures") or []:
            remaining = Money.from_payload(capture["amount"]) - Money.from_payload(
                capture["refunded_amount"]
            )
            captured = Money.from_payload(capture["amount"]).format()
            lines.append(
                f"  capture {capture['capture_id']}: {captured}, "
                f"unrefunded {remaining.format()}, refundable {capture['refundable']}"
            )
        duplicates = state.get("duplicate_captures") or []
        if duplicates:
            lines.append(f"  duplicate captures (retry artifacts): {', '.join(duplicates)}")

    # Dates are rendered here for the same reason amounts are: the drafter
    # must copy a fact, not compute its presentation. Handed a raw
    # `2026-10-17T10:00:00Z`, a model writes whatever reads well to it, and
    # which day that is depends on a zone nobody chose.
    for settlement in state.get("settlements") or []:
        lines.append(
            f"settlement {settlement['settlement_id']}: {settlement['status']}, "
            f"expected {format_day(settlement['expected_at'])}, "
            f"utr {settlement.get('utr') or 'not yet issued'}"
        )

    for incident in state.get("incidents") or []:
        expected = incident.get("expected_resolution_at")
        lines.append(
            f"active incident {incident['incident_id']}: {incident['title']}; "
            f"resolution date {format_day(expected) if expected else 'not committed'}"
        )

    packet = state.get("evidence_packet") or {}
    for chargeback in packet.get("chargebacks") or []:
        lines.append(
            f"chargeback {chargeback['chargeback_id']}: {chargeback['reason_code']}, "
            f"evidence due {format_day(chargeback['evidence_due_at'])}"
        )

    if state.get("repeat_contact"):
        lines.append(f"prior contacts: {len(state.get('contact_history') or [])}")

    return "\n".join(lines)
