"""Typed arguments for every tool.

Pydantic at the boundary, so an argument the model hallucinated is rejected
before it reaches a system of record rather than producing a confusing 400.

Two conventions hold throughout. ``MoneyArg`` forbids a float and converts to
the ``Money`` type on the way through, so no amount crosses this boundary as a
number the caller can do arithmetic on. And every model forbids extra fields:
an argument the tool does not understand is a sign the caller believes
something untrue about the tool, and silently dropping it hides that.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.shared.money import Money

MerchantId = Annotated[str, Field(pattern=r"^M-[0-9]{4}$")]
CaptureId = Annotated[str, Field(pattern=r"^cap_[A-Za-z0-9]+$")]
PaymentId = Annotated[str, Field(pattern=r"^pay_[A-Za-z0-9]+$")]
RefundId = Annotated[str, Field(pattern=r"^rfnd_[A-Za-z0-9]+$")]
TicketId = Annotated[str, Field(pattern=r"^tkt_[A-Za-z0-9]+$")]


class ToolArgs(BaseModel):
    """Base for every tool's arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class MoneyArg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    amount_minor: int
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @field_validator("amount_minor", mode="before")
    @classmethod
    def reject_float(cls, value: Any) -> Any:
        # Pydantic would happily coerce 2450.0 to 2450. It must not: a float
        # arriving here means someone upstream did arithmetic we cannot see.
        if isinstance(value, float):
            raise ValueError("amount_minor must be an integer; money is never a float")
        return value

    def to_money(self) -> Money:
        return Money(self.amount_minor, self.currency)


# -- CRM ---------------------------------------------------------------------


class GetMerchantArgs(ToolArgs):
    merchant_id: MerchantId


class GetContactHistoryArgs(ToolArgs):
    merchant_id: MerchantId
    limit: int = Field(default=20, ge=1, le=100)
    since: str | None = None


class AddMerchantNoteArgs(ToolArgs):
    merchant_id: MerchantId
    body: str = Field(min_length=1, max_length=4000)
    author: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


# -- Payments ----------------------------------------------------------------


class SearchPaymentsArgs(ToolArgs):
    merchant_id: MerchantId
    order_ref: str | None = None
    status: str | None = None
    created_after: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


class GetPaymentArgs(ToolArgs):
    payment_id: PaymentId


class GetCaptureArgs(ToolArgs):
    capture_id: CaptureId


class CreateRefundArgs(ToolArgs):
    """The only money-moving tool.

    ``capture_id`` and not ``payment_id``: the contract has no endpoint for
    refunding a payment, and the type system should not suggest otherwise.
    """

    capture_id: CaptureId
    amount: MoneyArg
    reason_code: Literal["duplicate", "buyer_request", "goods_not_delivered", "fraud", "goodwill"]
    initiated_by: Literal["agent", "human", "merchant"] = "agent"
    approval_id: str | None = None
    note: str | None = Field(default=None, max_length=1000)


class GetRefundArgs(ToolArgs):
    refund_id: RefundId


class SearchSettlementsArgs(ToolArgs):
    merchant_id: MerchantId
    status: str | None = None
    expected_after: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


class SearchChargebacksArgs(ToolArgs):
    merchant_id: MerchantId
    status: str | None = None
    capture_id: CaptureId | None = None


# -- Ticketing ---------------------------------------------------------------


class GetTicketArgs(ToolArgs):
    ticket_id: TicketId


class SetTicketStatusArgs(ToolArgs):
    ticket_id: TicketId
    status: Literal["open", "awaiting_customer", "awaiting_approval", "resolved", "escalated"]
    reason: str = Field(min_length=1, max_length=1000)
    run_id: str | None = None


class PostMessageArgs(ToolArgs):
    ticket_id: TicketId
    direction: Literal["outbound", "internal_note"]
    author: str = Field(min_length=1)
    body: str = Field(min_length=1, max_length=20000)
    citations: list[str] = Field(default_factory=list)


class AssignTicketArgs(ToolArgs):
    ticket_id: TicketId
    queue: Literal["general", "disputes", "compliance", "settlements", "technical"]
    reason: str = Field(min_length=1, max_length=1000)
    context: dict[str, Any] = Field(default_factory=dict)


class FlagTicketArgs(ToolArgs):
    ticket_id: TicketId
    flag: Literal["suspected_injection", "repeat_contact", "sla_breached", "compliance_sensitive"]


# -- Knowledge ---------------------------------------------------------------


class SearchKnowledgeArgs(ToolArgs):
    """``query`` carries untrusted text.

    It reaches the knowledge service in a request body rather than a URL, and
    it never becomes part of a prompt instruction. The type does not enforce
    that on its own; the prompt assembler does. What the type does enforce is
    a length bound, so a 20,000-character ticket cannot be used as a
    retrieval denial-of-service.
    """

    query: str = Field(min_length=1, max_length=2000)
    tenant_id: str
    top_k: int = Field(default=6, ge=1, le=20)
    doc_types: list[str] = Field(default_factory=list)
    min_score: float | None = Field(default=None, ge=0, le=1)


class GetPassageArgs(ToolArgs):
    passage_id: str = Field(pattern=r"^psg_[A-Za-z0-9_.-]+$")


class GetActiveIncidentsArgs(ToolArgs):
    tenant_id: str
    affects: Literal["payments", "settlements", "payouts", "dashboard", "webhooks"] | None = None
