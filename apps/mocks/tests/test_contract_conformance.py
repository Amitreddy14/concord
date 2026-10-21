"""Conformance of each mock to the contract it claims to implement.

These are shape checks, not behaviour checks: every operation the contract
declares exists, responds at the declared status, and returns the required
fields. Behaviour lives in the other test modules.

The value here is that the tool layer in phase 2 is generated from the
contracts. A mock that quietly omits a field the contract declares produces a
client that works against the mock and fails against anything else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from apps.mocks.tests.conftest import seed
from evals.fixtures import Scenario

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def contract(name: str) -> dict[str, Any]:
    return yaml.safe_load((CONTRACTS / f"{name}.yaml").read_text(encoding="utf-8"))


def required_fields(spec: dict[str, Any], schema_name: str) -> list[str]:
    schema = spec["components"]["schemas"][schema_name]
    return list(schema.get("required", []))


KEY = {"Idempotency-Key": "test-key-0123456789abcdef"}


class TestHealth:
    @pytest.mark.parametrize("service", ["crm", "payments", "ticketing", "knowledge"])
    def test_every_service_exposes_health(self, service: str, request: Any) -> None:
        client: TestClient = request.getfixturevalue(service)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestCrmConformance:
    def test_merchant_carries_every_required_field(
        self, crm: TestClient, scenarios: dict[str, Scenario]
    ) -> None:
        seed(crm, scenarios["S02"])
        body = crm.get("/merchants/M-1007").json()
        for field in required_fields(contract("crm"), "Merchant"):
            assert field in body, f"contract requires Merchant.{field}"

    def test_unknown_merchant_is_a_problem_document(self, crm: TestClient) -> None:
        response = crm.get("/merchants/M-9999")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        for field in ("type", "title", "status", "code"):
            assert field in response.json()

    def test_note_requires_an_idempotency_key(
        self, crm: TestClient, scenarios: dict[str, Scenario]
    ) -> None:
        seed(crm, scenarios["S02"])
        response = crm.post(
            "/merchants/M-1007/notes", json={"body": "x", "author": "run_1"}
        )
        assert response.status_code == 422  # FastAPI rejects the missing header


class TestPaymentsConformance:
    def test_payment_carries_every_required_field(
        self, payments: TestClient, scenarios: dict[str, Scenario]
    ) -> None:
        seed(payments, scenarios["S02"])
        body = payments.get("/payments/pay_8842A").json()
        for field in required_fields(contract("payments"), "Payment"):
            assert field in body, f"contract requires Payment.{field}"

    def test_capture_carries_every_required_field(
        self, payments: TestClient, scenarios: dict[str, Scenario]
    ) -> None:
        seed(payments, scenarios["S02"])
        body = payments.get("/captures/cap_8842A1").json()
        for field in required_fields(contract("payments"), "Capture"):
            assert field in body, f"contract requires Capture.{field}"

    def test_refund_carries_every_required_field(
        self, payments: TestClient, scenarios: dict[str, Scenario]
    ) -> None:
        seed(payments, scenarios["S03"])
        response = payments.post(
            "/refunds",
            json={
                "capture_id": "cap_9120B1",
                "amount": {"amount_minor": 89000, "currency": "INR"},
                "reason_code": "buyer_request",
                "initiated_by": "agent",
            },
            headers=KEY,
        )
        assert response.status_code == 201
        for field in required_fields(contract("payments"), "Refund"):
            assert field in response.json(), f"contract requires Refund.{field}"

    def test_money_is_always_an_object_never_a_number(
        self, payments: TestClient, scenarios: dict[str, Scenario]
    ) -> None:
        seed(payments, scenarios["S07"])
        settlement = payments.get("/settlements", params={"merchantId": "M-1020"}).json()["items"][0]
        for field in ("gross_amount", "fees", "refunds_deducted", "net_amount"):
            assert isinstance(settlement[field], dict), f"{field} must be a Money object"
            assert isinstance(settlement[field]["amount_minor"], int)
            assert not isinstance(settlement[field]["amount_minor"], bool)

    def test_search_requires_a_merchant_scope(self, payments: TestClient) -> None:
        # No cross-merchant search path exists, so a ticket referencing another
        # merchant's order cannot reach it.
        assert payments.get("/payments").status_code == 422


class TestTicketingConformance:
    def test_ticket_carries_every_required_field(
        self, ticketing: TestClient, scenarios: dict[str, Scenario]
    ) -> None:
        seed(ticketing, scenarios["S02"])
        response = ticketing.post(
            "/tickets",
            json={
                "merchant_id": "M-1007",
                "channel": "email",
                "subject": "s",
                "body": "b",
            },
            headers=KEY,
        )
        assert response.status_code == 201
        for field in required_fields(contract("ticketing"), "Ticket"):
            assert field in response.json(), f"contract requires Ticket.{field}"


class TestKnowledgeConformance:
    def test_passage_carries_every_required_field(
        self, knowledge: TestClient, scenarios: dict[str, Scenario]
    ) -> None:
        seed(knowledge, scenarios["S01"])
        response = knowledge.post(
            "/search", json={"query": "how long do refunds take", "tenant_id": "acme-payments"}
        )
        assert response.status_code == 200
        passages = response.json()["passages"]
        assert passages, "the S01 corpus should match a refund-timing query"
        for field in required_fields(contract("knowledge"), "Passage"):
            assert field in passages[0], f"contract requires Passage.{field}"

    def test_search_is_a_post_and_takes_no_idempotency_key(self) -> None:
        # Declared x-concord-read-only in the contract: a POST because the
        # query is untrusted and must not travel in a URL, but it mutates
        # nothing, so demanding a key would be wrong.
        spec = contract("knowledge")
        operation = spec["paths"]["/search"]["post"]
        assert operation.get("x-concord-read-only") is True
        rendered = yaml.safe_dump(operation.get("parameters", []))
        assert "IdempotencyKey" not in rendered
