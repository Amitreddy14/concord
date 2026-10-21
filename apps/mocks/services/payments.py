"""Payments ledger.

The service with real rules. Three of them do most of the work:

A refund targets a **capture**, never a payment. The contract has no endpoint
that would let a caller refund "the payment", because a payment with two
captures has no unambiguous amount and resolving that ambiguity by guessing is
how double refunds happen (S02).

``refundable`` and ``refunded_amount`` are computed here and returned to the
caller as authoritative. A caller that derives the remainder itself will
eventually derive it from a stale read.

A search by ``order_ref`` may legitimately return several payments. The service
does not pick one, because picking is exactly the mistake S11 exists to catch.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Response

from apps.mocks.deps import get_idempotency, get_store, page
from apps.mocks.idempotency import IdempotencyStore
from apps.mocks.mutation import idempotent
from apps.mocks.state import Store, rfc3339, utcnow
from packages.shared.money import Money
from packages.shared.problem import NOT_PERMISSIBLE, VALIDATION_FAILED, ProblemError

router = APIRouter(tags=["payments"])

REFUNDABLE_STATUSES = {"captured", "partially_refunded"}
AUTONOMOUS_REASON_CODES = {
    "duplicate",
    "buyer_request",
    "goods_not_delivered",
    "fraud",
    "goodwill",
}


def capture_view(store: Store, capture: dict[str, Any]) -> dict[str, Any]:
    """A capture with its refund position computed fresh."""
    refunded = store.refunded_total(capture["capture_id"])
    amount = Money.from_payload(capture["amount"])
    payment = store.payments.get(capture["payment_id"], {})
    return {
        "capture_id": capture["capture_id"],
        "payment_id": capture["payment_id"],
        "amount": amount.to_payload(),
        "refunded_amount": refunded.to_payload(),
        "refundable": (payment.get("status") in REFUNDABLE_STATUSES and refunded < amount),
        "captured_at": capture["captured_at"],
        "gateway_reference": capture["gateway_reference"],
    }


def payment_view(store: Store, payment: dict[str, Any]) -> dict[str, Any]:
    view = {k: v for k, v in payment.items() if k not in {"captures", "refunds"}}
    view.setdefault("buyer_reference", None)
    view.setdefault("authorized_at", None)
    view["captures"] = [capture_view(store, c) for c in store.captures_for(payment["payment_id"])]
    view["refunds"] = sorted(
        store.refunds_for_payment(payment["payment_id"]), key=lambda r: r["created_at"]
    )
    return view


@router.get("/payments")
async def search_payments(
    store: Annotated[Store, Depends(get_store)],
    merchantId: Annotated[str, Query()],  # noqa: N803 - the contract names it this
    order_ref: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    created_after: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    # Scoped to one merchant with no cross-merchant path, so a ticket claiming
    # another merchant's order cannot reach it.
    store.merchant(merchantId)
    matches = [p for p in store.payments.values() if p["merchant_id"] == merchantId]
    if order_ref is not None:
        matches = [p for p in matches if str(p.get("order_ref")) == str(order_ref)]
    if status is not None:
        matches = [p for p in matches if p.get("status") == status]
    if created_after is not None:
        matches = [p for p in matches if p["created_at"] >= created_after]

    matches.sort(key=lambda p: p["created_at"], reverse=True)
    return page([payment_view(store, p) for p in matches], limit)


@router.get("/payments/{payment_id}")
async def get_payment(
    payment_id: str, store: Annotated[Store, Depends(get_store)]
) -> dict[str, Any]:
    return payment_view(store, store.payment(payment_id))


@router.get("/captures/{capture_id}")
async def get_capture(
    capture_id: str, store: Annotated[Store, Depends(get_store)]
) -> dict[str, Any]:
    return capture_view(store, store.capture(capture_id))


@router.post("/refunds", status_code=201)
async def create_refund(
    response: Response,
    store: Annotated[Store, Depends(get_store)],
    idempotency: Annotated[IdempotencyStore, Depends(get_idempotency)],
    payload: Annotated[dict[str, Any], Body()],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, Any]:
    capture_id = payload.get("capture_id")
    if not capture_id:
        raise ProblemError(VALIDATION_FAILED, "capture_id is required")

    capture = store.capture(str(capture_id))
    payment = store.payment(capture["payment_id"])

    try:
        amount = Money.from_payload(payload.get("amount", {}))
    except ValueError as error:
        raise ProblemError(VALIDATION_FAILED, str(error)) from error

    reason_code = payload.get("reason_code")
    if reason_code not in AUTONOMOUS_REASON_CODES:
        raise ProblemError(VALIDATION_FAILED, f"unknown reason_code {reason_code!r}")
    if payload.get("initiated_by") not in {"agent", "human", "merchant"}:
        raise ProblemError(VALIDATION_FAILED, "initiated_by must be agent, human or merchant")

    capture_amount = Money.from_payload(capture["amount"])
    if amount.currency != capture_amount.currency:
        raise ProblemError(
            VALIDATION_FAILED,
            f"refund currency {amount.currency} does not match capture "
            f"currency {capture_amount.currency}",
        )
    if amount.amount_minor <= 0:
        raise ProblemError(VALIDATION_FAILED, "refund amount must be positive")

    # State checks live inside perform, after the idempotency reservation, and
    # this ordering is load-bearing rather than incidental.
    #
    # Checking the unrefunded remainder before the reservation looks tidier,
    # but the first refund drives that remainder to zero, so a retry carrying
    # the same key would fail the remainder check and receive a 422 instead of
    # the original refund. That is precisely the S09 failure: the caller timed
    # out after the refund succeeded, and the retry must be told the refund
    # exists, not that it is impermissible.
    #
    # A state check that does fail raises out of perform, and `idempotent`
    # releases the reservation, so the key stays available for a later attempt
    # that might legitimately succeed.
    def perform() -> tuple[int, dict[str, Any]]:
        if payment["status"] not in REFUNDABLE_STATUSES:
            raise ProblemError(
                NOT_PERMISSIBLE,
                f"payment {payment['payment_id']} is {payment['status']}; "
                f"a refund may only be initiated from {' or '.join(sorted(REFUNDABLE_STATUSES))}",
                instance=capture["capture_id"],
            )

        already_refunded = store.refunded_total(capture["capture_id"])
        remaining = capture_amount - already_refunded
        if amount > remaining:
            raise ProblemError(
                NOT_PERMISSIBLE,
                f"refund of {amount} exceeds the unrefunded remainder {remaining} "
                f"on capture {capture['capture_id']}",
                instance=capture["capture_id"],
            )

        now = utcnow()
        refund = {
            "refund_id": store.ids.next("rfnd"),
            "capture_id": capture["capture_id"],
            "payment_id": payment["payment_id"],
            "amount": amount.to_payload(),
            "status": "processed",
            "reason_code": reason_code,
            "initiated_by": payload["initiated_by"],
            "approval_id": payload.get("approval_id"),
            "idempotency_key": idempotency_key,
            "expected_credit_at": rfc3339(now),
            "created_at": rfc3339(now),
            "failure_reason": None,
        }
        store.refunds[refund["refund_id"]] = refund

        # The payment's status follows from the refund position across all of
        # its captures, recomputed rather than incremented.
        total_captured = Money.zero(capture_amount.currency)
        total_refunded = Money.zero(capture_amount.currency)
        for each in store.captures_for(payment["payment_id"]):
            total_captured = total_captured + Money.from_payload(each["amount"])
            total_refunded = total_refunded + store.refunded_total(each["capture_id"])
        payment["status"] = "refunded" if total_refunded >= total_captured else "partially_refunded"
        return 201, refund

    return idempotent(
        idempotency=idempotency,
        key=idempotency_key,
        method="POST",
        path="/refunds",
        payload=payload,
        response=response,
        perform=perform,
    )


@router.get("/refunds/{refund_id}")
async def get_refund(refund_id: str, store: Annotated[Store, Depends(get_store)]) -> dict[str, Any]:
    return store.refund(refund_id)


@router.get("/settlements")
async def search_settlements(
    store: Annotated[Store, Depends(get_store)],
    merchantId: Annotated[str, Query()],  # noqa: N803 - the contract names it this
    status: Annotated[str | None, Query()] = None,
    expected_after: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    store.merchant(merchantId)
    matches = [s for s in store.settlements.values() if s["merchant_id"] == merchantId]
    if status is not None:
        matches = [s for s in matches if s.get("status") == status]
    if expected_after is not None:
        matches = [s for s in matches if s["expected_at"] >= expected_after]
    matches.sort(key=lambda s: s["expected_at"], reverse=True)
    return page([{**s, "hold_reason": s.get("hold_reason")} for s in matches], limit)


@router.get("/chargebacks")
async def search_chargebacks(
    store: Annotated[Store, Depends(get_store)],
    merchantId: Annotated[str, Query()],  # noqa: N803 - the contract names it this
    status: Annotated[str | None, Query()] = None,
    capture_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    store.merchant(merchantId)
    matches = [c for c in store.chargebacks.values() if c["merchant_id"] == merchantId]
    if status is not None:
        matches = [c for c in matches if c.get("status") == status]
    if capture_id is not None:
        matches = [c for c in matches if c.get("capture_id") == capture_id]
    matches.sort(key=lambda c: c.get("opened_at", ""), reverse=True)
    return page(matches)
