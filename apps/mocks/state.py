"""In-memory state shared by the mock systems of record.

Real state, not canned responses: a refund changes the capture it targets, a
ticket transition is rejected if it is illegal, and a second capture on one
payment is a thing the ledger actually holds. ADR 0002 gives the reason — a
stateless mock would let the agent appear to work while never exercising the
cases the project exists to handle.

The store is a plain dictionary of records rather than an ORM. The data is
small, the process is single-threaded per service, and a schema here would be
a second source of truth competing with the contracts.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from packages.shared.money import Money
from packages.shared.problem import not_found


def utcnow() -> datetime:
    return datetime.now(UTC)


def rfc3339(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def normalise(value: Any) -> Any:
    """Convert YAML-parsed temporals to the strings the contracts specify.

    PyYAML turns an unquoted ISO timestamp into a ``datetime`` and a bare date
    into a ``date``. The contracts say these fields are RFC 3339 strings, so
    normalising on the way in keeps one representation in the store rather
    than leaving every handler to cope with both.
    """
    if isinstance(value, datetime):
        return rfc3339(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: normalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalise(v) for v in value]
    return value


class IdFactory:
    """Sequential ids with a stable prefix.

    Sequential rather than random so a failing test names the same id on every
    run, which makes a diff of two runs readable.
    """

    def __init__(self) -> None:
        self._counters: dict[str, itertools.count[int]] = {}

    def reset(self) -> None:
        self._counters.clear()

    def next(self, prefix: str) -> str:
        counter = self._counters.setdefault(prefix, itertools.count(1))
        return f"{prefix}_{next(counter):06d}"


@dataclass(slots=True)
class Store:
    """Every record the four services hold, keyed by id."""

    merchants: dict[str, dict[str, Any]] = field(default_factory=dict)
    contact_history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    notes: dict[str, dict[str, Any]] = field(default_factory=dict)

    payments: dict[str, dict[str, Any]] = field(default_factory=dict)
    captures: dict[str, dict[str, Any]] = field(default_factory=dict)
    refunds: dict[str, dict[str, Any]] = field(default_factory=dict)
    settlements: dict[str, dict[str, Any]] = field(default_factory=dict)
    chargebacks: dict[str, dict[str, Any]] = field(default_factory=dict)

    tickets: dict[str, dict[str, Any]] = field(default_factory=dict)
    messages: dict[str, dict[str, Any]] = field(default_factory=dict)

    passages: dict[str, dict[str, Any]] = field(default_factory=dict)
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    incidents: dict[str, dict[str, Any]] = field(default_factory=dict)

    ids: IdFactory = field(default_factory=IdFactory)

    # -- lifecycle -------------------------------------------------------

    def reset(self) -> None:
        for collection in (
            self.merchants,
            self.contact_history,
            self.notes,
            self.payments,
            self.captures,
            self.refunds,
            self.settlements,
            self.chargebacks,
            self.tickets,
            self.messages,
            self.passages,
            self.documents,
            self.incidents,
        ):
            collection.clear()
        self.ids.reset()

    def load(self, seed: dict[str, Any]) -> None:
        """Load a fixture's ``seed`` block.

        Captures are denormalised out of their payments into their own index so
        a refund can be looked up by capture id without scanning, which is the
        access pattern the contract's capture endpoint implies.
        """
        seed = normalise(seed)

        crm = seed.get("crm", {})
        for merchant in crm.get("merchants", []):
            self.merchants[merchant["merchant_id"]] = dict(merchant)
        for record in crm.get("contact_history", []):
            merchant_id = record.get("merchant_id") or next(iter(self.merchants), None)
            if merchant_id:
                self.contact_history.setdefault(merchant_id, []).append(dict(record))

        payments = seed.get("payments", {})
        for payment in payments.get("payments", []):
            record = dict(payment)
            captures = record.pop("captures", [])
            refunds = record.pop("refunds", [])
            self.payments[record["payment_id"]] = record
            for capture in captures:
                self.captures[capture["capture_id"]] = dict(capture)
            for refund in refunds:
                self.refunds[refund["refund_id"]] = dict(refund)
        for settlement in payments.get("settlements", []):
            self.settlements[settlement["settlement_id"]] = dict(settlement)
        for chargeback in payments.get("chargebacks", []):
            self.chargebacks[chargeback["chargeback_id"]] = dict(chargeback)

        knowledge = seed.get("knowledge", {})
        for passage in knowledge.get("passages", []):
            self.passages[passage["passage_id"]] = dict(passage)
            document_id = passage.get("document_id")
            if document_id and document_id not in self.documents:
                self.documents[document_id] = {
                    "document_id": document_id,
                    "doc_type": passage.get("doc_type", "policy"),
                    "title": passage.get("title", document_id),
                    "version": passage.get("version", "v1"),
                    "updated_at": rfc3339(utcnow()),
                    "passage_count": 0,
                }
        for document_id in self.documents:
            self.documents[document_id]["passage_count"] = sum(
                1 for p in self.passages.values() if p.get("document_id") == document_id
            )
        for incident in knowledge.get("incidents", []):
            self.incidents[incident["incident_id"]] = dict(incident)

        for ticket in seed.get("ticketing", {}).get("tickets", []):
            self.tickets[ticket["ticket_id"]] = dict(ticket)

    # -- typed accessors -------------------------------------------------
    # Each raises a problem rather than returning None, so a handler reads as
    # the happy path and a missing record cannot be silently treated as empty.

    def merchant(self, merchant_id: str) -> dict[str, Any]:
        record = self.merchants.get(merchant_id)
        if record is None:
            raise not_found("merchant", merchant_id)
        return record

    def payment(self, payment_id: str) -> dict[str, Any]:
        record = self.payments.get(payment_id)
        if record is None:
            raise not_found("payment", payment_id)
        return record

    def capture(self, capture_id: str) -> dict[str, Any]:
        record = self.captures.get(capture_id)
        if record is None:
            raise not_found("capture", capture_id)
        return record

    def refund(self, refund_id: str) -> dict[str, Any]:
        record = self.refunds.get(refund_id)
        if record is None:
            raise not_found("refund", refund_id)
        return record

    def ticket(self, ticket_id: str) -> dict[str, Any]:
        record = self.tickets.get(ticket_id)
        if record is None:
            raise not_found("ticket", ticket_id)
        return record

    def passage(self, passage_id: str) -> dict[str, Any]:
        record = self.passages.get(passage_id)
        if record is None:
            raise not_found("passage", passage_id)
        return record

    # -- derived views ---------------------------------------------------

    def captures_for(self, payment_id: str) -> list[dict[str, Any]]:
        return sorted(
            (c for c in self.captures.values() if c["payment_id"] == payment_id),
            key=lambda c: c["captured_at"],
        )

    def refunds_for_capture(self, capture_id: str) -> list[dict[str, Any]]:
        return [r for r in self.refunds.values() if r["capture_id"] == capture_id]

    def refunds_for_payment(self, payment_id: str) -> list[dict[str, Any]]:
        return [r for r in self.refunds.values() if r.get("payment_id") == payment_id]

    def messages_for(self, ticket_id: str) -> list[dict[str, Any]]:
        return sorted(
            (m for m in self.messages.values() if m["ticket_id"] == ticket_id),
            key=lambda m: m["created_at"],
        )

    def refunded_total(self, capture_id: str) -> Money:
        """Sum of refunds against a capture, excluding failed ones.

        Computed rather than stored. A denormalised running total is a second
        source of truth that drifts the first time a refund fails after the
        total has already been incremented.
        """
        capture = self.capture(capture_id)
        currency = capture["amount"]["currency"]
        total = Money.zero(currency)
        for refund in self.refunds_for_capture(capture_id):
            if refund.get("status") == "failed":
                continue
            total = total + Money.from_payload(refund["amount"])
        return total
