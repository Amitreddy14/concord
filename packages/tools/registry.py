"""The tool registry.

One declaration per tool, carrying everything the invoker needs to call it
safely: which service, which scope it requires, whether it mutates, how hard
to retry, and the Pydantic model its arguments must satisfy.

Declarative rather than a method per tool because the invoker's guarantees —
scope enforced before the network, keys derived for mutations, the ledger
written around every call — have to hold for *every* tool. A per-tool method
is a per-tool opportunity to forget one of them.

The scope strings match `tool_scopes` in the tenant configuration exactly. A
tool whose scope is absent from a tenant's list cannot be called in that
deployment, and that is the mechanism, not a convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.tools import models
from packages.tools.retry import RetryClass


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    service: str
    method: str
    path: str
    """Path template. Placeholders are filled from the validated arguments."""

    scope: str
    mutating: bool
    args_model: type[models.ToolArgs]
    retry_class: RetryClass
    summary: str

    query_params: tuple[str, ...] = ()
    """Argument names that travel as query parameters rather than a body."""

    path_params: tuple[str, ...] = ()
    """Argument names consumed by the path template."""

    query_aliases: dict[str, str] | None = None
    """Argument name to wire name, where the contract differs from ours."""

    def resolve_path(self, arguments: dict[str, Any]) -> str:
        values = {name: arguments[name] for name in self.path_params}
        return self.path.format(**values)

    def split_arguments(
        self, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Partition validated arguments into query parameters and a body."""
        aliases = self.query_aliases or {}
        query = {
            aliases.get(name, name): arguments[name]
            for name in self.query_params
            if arguments.get(name) is not None
        }
        consumed = set(self.path_params) | set(self.query_params)
        body = {k: v for k, v in arguments.items() if k not in consumed}
        if self.method == "GET" or not body:
            return query, None
        return query, body


def _spec(**kwargs: Any) -> ToolSpec:
    return ToolSpec(**kwargs)


TOOLS: dict[str, ToolSpec] = {
    # -- CRM -----------------------------------------------------------
    "get_merchant": _spec(
        name="get_merchant",
        service="crm",
        method="GET",
        path="/merchants/{merchant_id}",
        scope="crm:merchant:read",
        mutating=False,
        args_model=models.GetMerchantArgs,
        retry_class=RetryClass.READ,
        path_params=("merchant_id",),
        summary="Fetch a merchant's tier, KYC status and payout hold.",
    ),
    "get_contact_history": _spec(
        name="get_contact_history",
        service="crm",
        method="GET",
        path="/merchants/{merchant_id}/contact-history",
        scope="crm:merchant:read",
        mutating=False,
        args_model=models.GetContactHistoryArgs,
        retry_class=RetryClass.READ,
        path_params=("merchant_id",),
        query_params=("limit", "since"),
        summary="Prior support contacts, to establish whether this is a repeat.",
    ),
    "add_merchant_note": _spec(
        name="add_merchant_note",
        service="crm",
        method="POST",
        path="/merchants/{merchant_id}/notes",
        scope="crm:note:write",
        mutating=True,
        args_model=models.AddMerchantNoteArgs,
        retry_class=RetryClass.MUTATION,
        path_params=("merchant_id",),
        summary="Append an immutable note to the merchant record.",
    ),
    # -- Payments ------------------------------------------------------
    "search_payments": _spec(
        name="search_payments",
        service="payments",
        method="GET",
        path="/payments",
        scope="payments:payment:read",
        mutating=False,
        args_model=models.SearchPaymentsArgs,
        retry_class=RetryClass.READ,
        query_params=("merchant_id", "order_ref", "status", "created_after", "limit"),
        query_aliases={"merchant_id": "merchantId"},
        summary="Search a merchant's payments. May return several for one order_ref.",
    ),
    "get_payment": _spec(
        name="get_payment",
        service="payments",
        method="GET",
        path="/payments/{payment_id}",
        scope="payments:payment:read",
        mutating=False,
        args_model=models.GetPaymentArgs,
        retry_class=RetryClass.READ,
        path_params=("payment_id",),
        summary="Fetch a payment with its captures and refunds.",
    ),
    "get_capture": _spec(
        name="get_capture",
        service="payments",
        method="GET",
        path="/captures/{capture_id}",
        scope="payments:capture:read",
        mutating=False,
        args_model=models.GetCaptureArgs,
        retry_class=RetryClass.READ,
        path_params=("capture_id",),
        summary="Fetch a capture with its authoritative refundable remainder.",
    ),
    "create_refund": _spec(
        name="create_refund",
        service="payments",
        method="POST",
        path="/refunds",
        scope="payments:refund:write",
        mutating=True,
        args_model=models.CreateRefundArgs,
        retry_class=RetryClass.MUTATION,
        summary="Refund a capture. The only tool that moves money.",
    ),
    "get_refund": _spec(
        name="get_refund",
        service="payments",
        method="GET",
        path="/refunds/{refund_id}",
        scope="payments:refund:read",
        mutating=False,
        args_model=models.GetRefundArgs,
        retry_class=RetryClass.READ,
        path_params=("refund_id",),
        summary="Fetch a refund. Used by reconciliation after an indeterminate failure.",
    ),
    "search_settlements": _spec(
        name="search_settlements",
        service="payments",
        method="GET",
        path="/settlements",
        scope="payments:settlement:read",
        mutating=False,
        args_model=models.SearchSettlementsArgs,
        retry_class=RetryClass.READ,
        query_params=("merchant_id", "status", "expected_after", "limit"),
        query_aliases={"merchant_id": "merchantId"},
        summary="Settlements for a merchant, with status and expected date.",
    ),
    "search_chargebacks": _spec(
        name="search_chargebacks",
        service="payments",
        method="GET",
        path="/chargebacks",
        scope="payments:chargeback:read",
        mutating=False,
        args_model=models.SearchChargebacksArgs,
        retry_class=RetryClass.READ,
        query_params=("merchant_id", "status", "capture_id"),
        query_aliases={"merchant_id": "merchantId"},
        summary="Open disputes and their evidence deadlines.",
    ),
    # -- Ticketing -----------------------------------------------------
    "get_ticket": _spec(
        name="get_ticket",
        service="ticketing",
        method="GET",
        path="/tickets/{ticket_id}",
        scope="ticketing:ticket:read",
        mutating=False,
        args_model=models.GetTicketArgs,
        retry_class=RetryClass.READ,
        path_params=("ticket_id",),
        summary="Fetch a ticket with its message thread.",
    ),
    "set_ticket_status": _spec(
        name="set_ticket_status",
        service="ticketing",
        method="PATCH",
        path="/tickets/{ticket_id}/status",
        scope="ticketing:ticket:write",
        mutating=True,
        args_model=models.SetTicketStatusArgs,
        retry_class=RetryClass.MUTATION,
        path_params=("ticket_id",),
        summary="Transition a ticket. Illegal transitions are refused.",
    ),
    "post_message": _spec(
        name="post_message",
        service="ticketing",
        method="POST",
        path="/tickets/{ticket_id}/messages",
        scope="ticketing:message:write",
        mutating=True,
        args_model=models.PostMessageArgs,
        retry_class=RetryClass.MUTATION,
        path_params=("ticket_id",),
        summary="Append an outbound reply or an internal note.",
    ),
    "assign_ticket": _spec(
        name="assign_ticket",
        service="ticketing",
        method="POST",
        path="/tickets/{ticket_id}/assign",
        scope="ticketing:assign:write",
        mutating=True,
        args_model=models.AssignTicketArgs,
        retry_class=RetryClass.MUTATION,
        path_params=("ticket_id",),
        summary="Escalate a ticket to a human queue with structured evidence.",
    ),
    "flag_ticket": _spec(
        name="flag_ticket",
        service="ticketing",
        method="POST",
        path="/tickets/{ticket_id}/flags",
        scope="ticketing:ticket:write",
        mutating=True,
        args_model=models.FlagTicketArgs,
        retry_class=RetryClass.MUTATION,
        path_params=("ticket_id",),
        summary="Annotate a ticket for human attention.",
    ),
    # -- Knowledge -----------------------------------------------------
    "search_knowledge": _spec(
        name="search_knowledge",
        service="knowledge",
        method="POST",
        path="/search",
        scope="knowledge:search",
        mutating=False,
        args_model=models.SearchKnowledgeArgs,
        retry_class=RetryClass.READ,
        summary="Retrieve policy passages. A POST, but it mutates nothing.",
    ),
    "get_passage": _spec(
        name="get_passage",
        service="knowledge",
        method="GET",
        path="/passages/{passage_id}",
        scope="knowledge:search",
        mutating=False,
        args_model=models.GetPassageArgs,
        retry_class=RetryClass.READ,
        path_params=("passage_id",),
        summary="Fetch one passage, to verify a citation after the fact.",
    ),
    "get_active_incidents": _spec(
        name="get_active_incidents",
        service="knowledge",
        method="GET",
        path="/incidents/active",
        scope="knowledge:search",
        mutating=False,
        args_model=models.GetActiveIncidentsArgs,
        retry_class=RetryClass.READ,
        query_params=("tenant_id", "affects"),
        query_aliases={"tenant_id": "tenantId"},
        summary="Currently active platform incidents. Never returns resolved ones.",
    ),
}


def get(name: str) -> ToolSpec:
    spec = TOOLS.get(name)
    if spec is None:
        raise KeyError(f"no tool named {name!r}; registered: {', '.join(sorted(TOOLS))}")
    return spec


def mutating_tools() -> list[str]:
    return sorted(name for name, spec in TOOLS.items() if spec.mutating)


def required_scopes() -> set[str]:
    return {spec.scope for spec in TOOLS.values()}
