"""Merchant CRM.

Read-only to the agent apart from append-only notes. There is deliberately no
endpoint that changes a merchant's tier, KYC status or payout hold: S08 asserts
that the agent cannot lift a hold, and the strongest way to guarantee that is
for the capability not to exist rather than for a policy rule to forbid it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Response

from apps.mocks.deps import get_idempotency, get_store, page
from apps.mocks.idempotency import IdempotencyStore
from apps.mocks.mutation import idempotent
from apps.mocks.state import Store, rfc3339, utcnow
from packages.shared.problem import VALIDATION_FAILED, ProblemError

router = APIRouter(tags=["merchants"])


@router.get("/merchants/{merchant_id}")
async def get_merchant(
    merchant_id: str, store: Annotated[Store, Depends(get_store)]
) -> dict[str, Any]:
    merchant = dict(store.merchant(merchant_id))
    merchant.setdefault("support_contacts", [])
    merchant.setdefault("payout_hold_reason", None)
    return merchant


@router.get("/merchants/{merchant_id}/contact-history")
async def get_contact_history(
    merchant_id: str,
    store: Annotated[Store, Depends(get_store)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    since: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    store.merchant(merchant_id)
    records = list(store.contact_history.get(merchant_id, []))
    if since is not None:
        records = [r for r in records if r["occurred_at"] >= since]
    records.sort(key=lambda r: r["occurred_at"], reverse=True)
    return page([{k: v for k, v in r.items() if k != "merchant_id"} for r in records], limit)


@router.post("/merchants/{merchant_id}/notes", status_code=201)
async def add_merchant_note(
    merchant_id: str,
    response: Response,
    store: Annotated[Store, Depends(get_store)],
    idempotency: Annotated[IdempotencyStore, Depends(get_idempotency)],
    payload: Annotated[dict[str, Any], Body()],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, Any]:
    store.merchant(merchant_id)

    body = payload.get("body")
    author = payload.get("author")
    if not body or not isinstance(body, str):
        raise ProblemError(VALIDATION_FAILED, "body is required and must be a non-empty string")
    if not author or not isinstance(author, str):
        raise ProblemError(VALIDATION_FAILED, "author is required")

    def perform() -> tuple[int, dict[str, Any]]:
        note = {
            "note_id": store.ids.next("note"),
            "merchant_id": merchant_id,
            "body": body,
            "author": author,
            "tags": payload.get("tags", []),
            "created_at": rfc3339(utcnow()),
        }
        store.notes[note["note_id"]] = note
        return 201, note

    return idempotent(
        idempotency=idempotency,
        key=idempotency_key,
        method="POST",
        path=f"/merchants/{merchant_id}/notes",
        payload=payload,
        response=response,
        perform=perform,
    )
