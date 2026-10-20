"""Ticketing.

Two rules here are enforcement rather than bookkeeping.

``priority`` is derived by the service from merchant tier and is not writable.
A merchant who writes "URGENT" in a ticket body must not thereby raise their
own priority, and the reliable way to guarantee that is for no caller to be
able to set the field at all.

Illegal status transitions are rejected with 422 rather than accepted. A wrong
graph edge in the agent should surface as a failed call, not as a ticket in a
state the state machine says is unreachable.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Response

from apps.mocks.deps import get_idempotency, get_store
from apps.mocks.idempotency import IdempotencyStore
from apps.mocks.mutation import idempotent
from apps.mocks.state import Store, rfc3339, utcnow
from packages.shared.problem import (
    CITATIONS_REQUIRED,
    ILLEGAL_TRANSITION,
    VALIDATION_FAILED,
    ProblemError,
)

router = APIRouter(tags=["tickets"])

# The ticket state machine from the domain model, as adjacency. A transition
# absent from this table is not merely discouraged; it is refused.
TRANSITIONS: dict[str, set[str]] = {
    "open": {"awaiting_customer", "awaiting_approval", "resolved", "escalated"},
    "awaiting_customer": {"open", "resolved", "escalated"},
    "awaiting_approval": {"open", "resolved", "escalated"},
    "resolved": {"open"},
    "escalated": {"open", "resolved"},
}

PRIORITY_BY_TIER = {"standard": "normal", "growth": "high", "enterprise": "urgent"}
QUEUES = {"general", "disputes", "compliance", "settlements", "technical"}
VALID_FLAGS = {"suspected_injection", "repeat_contact", "sla_breached", "compliance_sensitive"}


def ticket_view(store: Store, ticket: dict[str, Any]) -> dict[str, Any]:
    view = dict(ticket)
    view["messages"] = store.messages_for(ticket["ticket_id"])
    view.setdefault("queue", None)
    view.setdefault("assignee", None)
    view.setdefault("sla_due_at", None)
    view.setdefault("resolved_at", None)
    view.setdefault("flags", [])
    return view


@router.post("/tickets", status_code=201)
async def create_ticket(
    response: Response,
    store: Annotated[Store, Depends(get_store)],
    idempotency: Annotated[IdempotencyStore, Depends(get_idempotency)],
    payload: Annotated[dict[str, Any], Body()],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, Any]:
    merchant_id = payload.get("merchant_id")
    if not merchant_id:
        raise ProblemError(VALIDATION_FAILED, "merchant_id is required")
    merchant = store.merchant(str(merchant_id))

    if payload.get("channel") not in {"chat", "email", "portal"}:
        raise ProblemError(VALIDATION_FAILED, "channel must be chat, email or portal")
    for field in ("subject", "body"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ProblemError(VALIDATION_FAILED, f"{field} is required")

    def perform() -> tuple[int, dict[str, Any]]:
        ticket = {
            "ticket_id": store.ids.next("tkt"),
            "merchant_id": merchant_id,
            "channel": payload["channel"],
            # Untrusted content, stored verbatim. Sanitising it here would
            # hide from the agent the thing the agent must handle safely.
            "subject": payload["subject"],
            "body": payload["body"],
            "status": "open",
            # Derived, never taken from the request.
            "priority": PRIORITY_BY_TIER.get(merchant.get("tier", "standard"), "normal"),
            "queue": None,
            "assignee": None,
            "sla_due_at": None,
            "created_at": rfc3339(utcnow()),
            "resolved_at": None,
            "flags": [],
        }
        store.tickets[ticket["ticket_id"]] = ticket
        store.messages[store.ids.next("msg")] = {
            "message_id": f"msg_inbound_{ticket['ticket_id']}",
            "ticket_id": ticket["ticket_id"],
            "direction": "inbound",
            "author": str(merchant_id),
            "body": payload["body"],
            "citations": [],
            "created_at": ticket["created_at"],
        }
        return 201, ticket_view(store, ticket)

    return idempotent(
        idempotency=idempotency,
        key=idempotency_key,
        method="POST",
        path="/tickets",
        payload=payload,
        response=response,
        perform=perform,
    )


@router.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: str, store: Annotated[Store, Depends(get_store)]) -> dict[str, Any]:
    return ticket_view(store, store.ticket(ticket_id))


@router.patch("/tickets/{ticket_id}/status")
async def set_ticket_status(
    ticket_id: str,
    response: Response,
    store: Annotated[Store, Depends(get_store)],
    idempotency: Annotated[IdempotencyStore, Depends(get_idempotency)],
    payload: Annotated[dict[str, Any], Body()],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, Any]:
    ticket = store.ticket(ticket_id)
    target = payload.get("status")
    if target not in TRANSITIONS:
        raise ProblemError(VALIDATION_FAILED, f"unknown status {target!r}")
    if not payload.get("reason"):
        raise ProblemError(VALIDATION_FAILED, "reason is required and is recorded on the ticket")

    current = ticket["status"]
    if target != current and target not in TRANSITIONS[current]:
        raise ProblemError(
            ILLEGAL_TRANSITION,
            f"ticket {ticket_id} cannot move from {current} to {target}; "
            f"permitted: {', '.join(sorted(TRANSITIONS[current]))}",
            instance=ticket_id,
        )

    def perform() -> tuple[int, dict[str, Any]]:
        ticket["status"] = target
        if target == "resolved":
            ticket["resolved_at"] = rfc3339(utcnow())
        store.messages[store.ids.next("msg")] = {
            "message_id": store.ids.next("msg"),
            "ticket_id": ticket_id,
            "direction": "internal_note",
            "author": payload.get("run_id") or "system",
            "body": f"status {current} -> {target}: {payload['reason']}",
            "citations": [],
            "created_at": rfc3339(utcnow()),
        }
        return 200, ticket_view(store, ticket)

    return idempotent(
        idempotency=idempotency,
        key=idempotency_key,
        method="PATCH",
        path=f"/tickets/{ticket_id}/status",
        payload=payload,
        response=response,
        perform=perform,
    )


@router.post("/tickets/{ticket_id}/messages", status_code=201)
async def post_message(
    ticket_id: str,
    response: Response,
    store: Annotated[Store, Depends(get_store)],
    idempotency: Annotated[IdempotencyStore, Depends(get_idempotency)],
    payload: Annotated[dict[str, Any], Body()],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    require_citations: Annotated[bool, Header(alias="X-Require-Citations")] = False,
) -> dict[str, Any]:
    store.ticket(ticket_id)
    direction = payload.get("direction")
    if direction not in {"outbound", "internal_note"}:
        raise ProblemError(VALIDATION_FAILED, "direction must be outbound or internal_note")
    if not payload.get("body"):
        raise ProblemError(VALIDATION_FAILED, "body is required")
    if not payload.get("author"):
        raise ProblemError(VALIDATION_FAILED, "author is required")

    # The tenant decides whether replies must be grounded; the header carries
    # that decision so the service does not need to read tenant configuration.
    citations = payload.get("citations") or []
    if direction == "outbound" and require_citations and not citations:
        raise ProblemError(
            CITATIONS_REQUIRED,
            "this tenant requires an outbound reply to cite at least one passage",
            instance=ticket_id,
        )

    def perform() -> tuple[int, dict[str, Any]]:
        message = {
            "message_id": store.ids.next("msg"),
            "ticket_id": ticket_id,
            "direction": direction,
            "author": payload["author"],
            "body": payload["body"],
            "citations": citations,
            "created_at": rfc3339(utcnow()),
        }
        store.messages[message["message_id"]] = message
        return 201, message

    return idempotent(
        idempotency=idempotency,
        key=idempotency_key,
        method="POST",
        path=f"/tickets/{ticket_id}/messages",
        payload=payload,
        response=response,
        perform=perform,
    )


@router.post("/tickets/{ticket_id}/assign")
async def assign_ticket(
    ticket_id: str,
    response: Response,
    store: Annotated[Store, Depends(get_store)],
    idempotency: Annotated[IdempotencyStore, Depends(get_idempotency)],
    payload: Annotated[dict[str, Any], Body()],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, Any]:
    ticket = store.ticket(ticket_id)
    queue = payload.get("queue")
    if queue not in QUEUES:
        raise ProblemError(VALIDATION_FAILED, f"queue must be one of {', '.join(sorted(QUEUES))}")
    if not payload.get("reason"):
        raise ProblemError(VALIDATION_FAILED, "reason is required")

    def perform() -> tuple[int, dict[str, Any]]:
        ticket["queue"] = queue
        ticket["assignee"] = f"{queue}-queue"
        if ticket["status"] == "open":
            ticket["status"] = "escalated"
        store.messages[store.ids.next("msg")] = {
            "message_id": store.ids.next("msg"),
            "ticket_id": ticket_id,
            "direction": "internal_note",
            "author": "system",
            "body": f"assigned to {queue}: {payload['reason']}",
            "citations": [],
            "created_at": rfc3339(utcnow()),
        }
        return 200, ticket_view(store, ticket)

    return idempotent(
        idempotency=idempotency,
        key=idempotency_key,
        method="POST",
        path=f"/tickets/{ticket_id}/assign",
        payload=payload,
        response=response,
        perform=perform,
    )


@router.post("/tickets/{ticket_id}/flags")
async def flag_ticket(
    ticket_id: str,
    store: Annotated[Store, Depends(get_store)],
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Annotate a ticket for human attention.

    Not in the contract as a mutation requiring a key because it is
    idempotent by construction: adding a flag that is already present is a
    no-op, so a retry cannot produce a different state.
    """
    ticket = store.ticket(ticket_id)
    flag = payload.get("flag")
    if flag not in VALID_FLAGS:
        raise ProblemError(VALIDATION_FAILED, f"unknown flag {flag!r}")
    flags = set(ticket.get("flags", []))
    flags.add(flag)
    ticket["flags"] = sorted(flags)
    return ticket_view(store, ticket)
