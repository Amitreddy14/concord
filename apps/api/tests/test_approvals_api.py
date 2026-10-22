"""The approval endpoints, over HTTP, against a run that really paused.

These tests drive the surface the reviewer console drives, and the thing they
are really checking is the ordering in ``approvals.record``: the decision is
persisted before the run is resumed. A test that only asserted "approving
produces a refund" would pass against the reverse ordering too, and the
reverse ordering loses a reviewer's answer on exactly the crash the durable
checkpointer exists to survive.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.main import Dependencies, SubmitTicket, create_app, execute
from apps.api.runs import RunStatus, RunStore
from apps.mocks.faults import FaultConfig, FaultProfile
from apps.mocks.serve import build
from evals.fixtures import load_all, reset_payload
from packages.policy.approval import ApprovalStatus, InMemoryApprovalStore
from packages.policy.tenant import load_tenant
from packages.shared.ledger import InMemoryLedger
from packages.shared.llm import ScriptedGateway
from packages.tools.tests.conftest import BASE_URLS, RoutingTransport

S04_TRIAGE = json.dumps(
    {
        "intent": "refund_request",
        "order_ref": "7731",
        "mentions_amount": False,
        "urgency_claimed": False,
        "suspected_injection": False,
        "summary": "Merchant reports a duplicated order and asks for a refund.",
    }
)
S04_DRAFT = json.dumps(
    {
        "reply": "I've raised that refund for review; you'll hear back shortly.",
        "citations": ["psg_refund-policy.v3.credit-timelines"],
        "unsupported_claims": [],
    }
)


@pytest.fixture
def deps() -> Iterator[Dependencies]:
    scenario = load_all()["S04"]
    services = {n: build(n, FaultConfig(profile=FaultProfile.OFF)) for n in BASE_URLS}
    payload = reset_payload(scenario)
    for app in services.values():
        with TestClient(app) as client:
            assert client.post("/_test/reset", json=payload).status_code == 200

    hosts = {
        httpx.URL(u).host: a for u, a in zip(BASE_URLS.values(), services.values(), strict=False)
    }
    load_tenant.cache_clear()
    dependencies = Dependencies(
        tenant=load_tenant("acme-payments"),
        gateway=ScriptedGateway(responses={"triage": [S04_TRIAGE] * 3, "draft": [S04_DRAFT] * 3}),
        base_urls=dict(BASE_URLS),
        client=httpx.AsyncClient(transport=RoutingTransport(hosts)),
        ledger=InMemoryLedger(),
        store=RunStore(),
        approvals=InMemoryApprovalStore(),
    )
    dependencies.services = services  # type: ignore[attr-defined]
    yield dependencies


SUBMISSION = {
    "merchant_id": "M-1012",
    "channel": "email",
    "subject": "Duplicate order 7731 - full refund needed",
    "body": "Order 7731 was duplicated on our end, please refund the full amount.",
    "ticket_id": "tkt_apr1",
}


def refunds(deps: Dependencies) -> list[dict[str, Any]]:
    return list(deps.services["payments"].state.store.refunds.values())  # type: ignore[attr-defined]


async def pause_a_run(deps: Dependencies) -> Any:
    run = deps.store.create(ticket_id="tkt_apr1", tenant_id="acme-payments", merchant_id="M-1012")
    await execute(run, deps, SubmitTicket(**SUBMISSION))
    assert run.status is RunStatus.AWAITING_APPROVAL, run.failure
    return run


@pytest.fixture
def client(deps: Dependencies) -> Iterator[TestClient]:
    with TestClient(create_app(deps)) as test_client:
        yield test_client


class TestQueue:
    async def test_a_paused_run_appears_in_the_queue_with_its_tier(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        await pause_a_run(deps)

        body = client.get("/approvals").json()
        assert body["count"] == 1
        row = body["approvals"][0]
        assert row["risk_tier"] == "R2-elevated-refund"
        assert row["required_scope"] == "payments:refund:write"
        assert row["proposed_action"]["amount"]["amount_minor"] == 4120000
        # A reviewer working the queue top-down needs to know what is about to
        # time out from under them.
        assert 0 < row["expires_in_seconds"] <= 48 * 3600

    async def test_an_unknown_approval_is_a_problem_document(self, client: TestClient) -> None:
        response = client.get("/approvals/apr_nope")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")


class TestDecisions:
    async def test_approving_records_then_resumes_and_refunds_once(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        run = await pause_a_run(deps)
        approval_id = deps.approvals.pending()[0].approval_id
        assert refunds(deps) == []

        response = client.post(
            f"/approvals/{approval_id}/decision",
            json={"action": "approve", "reviewer": "dana@acme"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["resumed"] is True

        stored = deps.approvals.get(approval_id)
        assert stored is not None
        assert stored.status is ApprovalStatus.APPROVED
        assert stored.reviewer == "dana@acme"

        # The resume runs as a background task; the run reaching a terminal
        # state is what says it finished.
        await _settle(run)
        assert run.status is RunStatus.COMPLETED
        assert len(refunds(deps)) == 1

    async def test_rejecting_requires_a_note_for_this_tenant(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        await pause_a_run(deps)
        approval_id = deps.approvals.pending()[0].approval_id

        response = client.post(
            f"/approvals/{approval_id}/decision",
            json={"action": "reject", "reviewer": "dana@acme"},
        )
        assert response.status_code == 400
        assert "note" in response.json()["detail"]

        # The failed decision left the request answerable, not half-decided.
        stored = deps.approvals.get(approval_id)
        assert stored is not None and stored.status is ApprovalStatus.PENDING

    async def test_a_reviewer_may_reduce_the_amount_but_not_raise_it(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        run = await pause_a_run(deps)
        approval_id = deps.approvals.pending()[0].approval_id

        too_much = client.post(
            f"/approvals/{approval_id}/decision",
            json={
                "action": "modify",
                "reviewer": "dana@acme",
                "note": "trying to refund more than was proposed",
                "amount_minor": 5000000,
            },
        )
        assert too_much.status_code == 400
        assert "not raise it" in too_much.json()["detail"]

        ok = client.post(
            f"/approvals/{approval_id}/decision",
            json={
                "action": "modify",
                "reviewer": "dana@acme",
                "note": "refunding half; the merchant kept one unit",
                "amount_minor": 2060000,
            },
        )
        assert ok.status_code == 200, ok.text

        await _settle(run)
        issued = refunds(deps)
        assert len(issued) == 1
        assert issued[0]["amount"]["amount_minor"] == 2060000

    async def test_a_second_reviewer_cannot_overwrite_a_recorded_decision(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        await pause_a_run(deps)
        approval_id = deps.approvals.pending()[0].approval_id

        first = client.post(
            f"/approvals/{approval_id}/decision",
            json={"action": "approve", "reviewer": "dana@acme"},
        )
        assert first.status_code == 200

        second = client.post(
            f"/approvals/{approval_id}/decision",
            json={
                "action": "reject",
                "reviewer": "sam@acme",
                "note": "I disagree with Dana",
            },
        )
        # 409: the request is well formed and the conflict is with the state
        # of the world, so a console knows to reload rather than retry.
        assert second.status_code == 409
        assert "already" in second.json()["detail"]
        assert "dana@acme" in second.json()["detail"]

    async def test_an_amount_may_not_accompany_an_approval(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        # Otherwise "approve with an amount" becomes an undocumented second
        # way to modify, bypassing the note the tenant requires on one.
        await pause_a_run(deps)
        approval_id = deps.approvals.pending()[0].approval_id

        response = client.post(
            f"/approvals/{approval_id}/decision",
            json={"action": "approve", "reviewer": "dana@acme", "amount_minor": 100},
        )
        assert response.status_code == 400
        assert "only accompany" in response.json()["detail"]


class TestSweep:
    async def test_the_sweep_expires_an_overdue_request_and_ends_the_run(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        run = await pause_a_run(deps)
        approval_id = deps.approvals.pending()[0].approval_id

        # Age the request rather than the clock: the deadline is what the
        # sweep reads, and moving it is the honest way to reach the branch.
        deps.approvals.reschedule(approval_id, datetime.now(UTC) - timedelta(hours=1))

        body = client.post("/approvals/sweep").json()
        assert body["expired"] == [approval_id]
        assert body["resumed"] == [approval_id]

        # The run is abandoned rather than failed: nothing broke, a human
        # simply never answered. And no money moved.
        assert run.status is RunStatus.ABANDONED
        assert refunds(deps) == []

    async def test_the_sweep_is_a_no_op_when_nothing_is_overdue(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        await pause_a_run(deps)
        body = client.post("/approvals/sweep").json()
        assert body == {"expired": [], "resumed": []}
        assert deps.approvals.pending()


async def _settle(run: Any, attempts: int = 200) -> None:
    """Wait for the background resume to reach a terminal run state."""
    import asyncio

    for _ in range(attempts):
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.ABANDONED}:
            return
        await asyncio.sleep(0.01)  # allow-float: a poll interval in seconds, not an amount
    raise AssertionError(f"run did not settle; still {run.status}")
