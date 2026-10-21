"""The async intake API.

The property that matters is that submitting returns immediately and progress
is observable afterwards. A synchronous endpoint would be simpler and wrong:
on local inference a resolution takes tens of seconds, and a client holding a
connection open through that is a client whose mobile network drops it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.main import Dependencies, SubmitTicket, create_app, execute
from apps.api.runs import RunStatus, RunStore
from apps.mocks.faults import FaultConfig, FaultProfile
from apps.mocks.serve import build
from evals.fixtures import load_all, reset_payload
from packages.agent.tests.conftest import S01_DRAFT, S01_TRIAGE
from packages.policy.tenant import load_tenant
from packages.shared.ledger import InMemoryLedger
from packages.shared.llm import ScriptedGateway
from packages.tools.tests.conftest import BASE_URLS, RoutingTransport

SUBMISSION = {
    "merchant_id": "M-1001",
    "channel": "email",
    "subject": "Refund timing question",
    "body": "How long do refunds take to reach the customer's account?",
    "ticket_id": "tkt_api1",
}


@pytest.fixture
def deps() -> Any:
    scenario = load_all()["S01"]
    services = {n: build(n, FaultConfig(profile=FaultProfile.OFF)) for n in BASE_URLS}
    payload = reset_payload(scenario)
    for app in services.values():
        with TestClient(app) as client:
            client.post("/_test/reset", json=payload)

    hosts = {
        httpx.URL(u).host: a for u, a in zip(BASE_URLS.values(), services.values(), strict=False)
    }
    load_tenant.cache_clear()
    return Dependencies(
        tenant=load_tenant("acme-payments"),
        gateway=ScriptedGateway(responses={"triage": [S01_TRIAGE], "draft": [S01_DRAFT]}),
        base_urls=dict(BASE_URLS),
        client=httpx.AsyncClient(transport=RoutingTransport(hosts)),
        ledger=InMemoryLedger(),
        store=RunStore(),
    )


class TestSubmission:
    async def test_submitting_returns_a_run_id_without_waiting(self, deps: Any) -> None:
        run = deps.store.create(
            ticket_id="tkt_api1", tenant_id="acme-payments", merchant_id="M-1001"
        )
        assert run.status is RunStatus.QUEUED
        assert run.run_id.startswith("run_")
        # Nothing has happened yet, which is the point: the caller is not
        # blocked on the graph.
        assert run.events == []

    async def test_the_run_completes_and_carries_its_result(self, deps: Any) -> None:
        run = deps.store.create(
            ticket_id="tkt_api1", tenant_id="acme-payments", merchant_id="M-1001"
        )
        await execute(run, deps, SubmitTicket(**SUBMISSION))

        assert run.status is RunStatus.COMPLETED
        assert run.result["intent"] == "status_question"
        assert run.result["visited"] == ["triage", "gather", "retrieve", "draft", "verify"]
        assert run.result["reply"]["citations"] == ["psg_refund-policy.v3.credit-timelines"]
        assert run.result["reply"]["is_refusal"] is False
        assert run.result["grounding"]["verdict"] == "supported"
        assert run.result["usage"]["calls"] == 2

    async def test_progress_events_name_each_node(self, deps: Any) -> None:
        run = deps.store.create(
            ticket_id="tkt_api1", tenant_id="acme-payments", merchant_id="M-1001"
        )
        await execute(run, deps, SubmitTicket(**SUBMISSION))

        kinds = [e.kind for e in run.events]
        assert kinds[0] == "run.started"
        assert kinds[-1] == "run.finished"
        nodes = [e.detail["node"] for e in run.events if e.kind == "node.completed"]
        assert nodes == ["triage", "gather", "retrieve", "draft", "verify"]

    async def test_a_failure_becomes_a_terminal_state_not_a_lost_task(self, deps: Any) -> None:
        # A background task that dies silently is worse than one that fails
        # loudly, because nobody knows to look.
        deps.gateway = ScriptedGateway(responses={})
        run = deps.store.create(
            ticket_id="tkt_api2", tenant_id="acme-payments", merchant_id="M-1001"
        )
        await execute(run, deps, SubmitTicket(**SUBMISSION))

        assert run.status is RunStatus.FAILED
        assert run.failure is not None
        assert run.events[-1].kind == "run.finished"


class TestStreaming:
    async def test_a_late_subscriber_receives_the_replay(self, deps: Any) -> None:
        # A client connecting a second after submitting would otherwise miss
        # the first node's events and see a run that starts halfway through.
        run = deps.store.create(
            ticket_id="tkt_api1", tenant_id="acme-payments", merchant_id="M-1001"
        )
        await execute(run, deps, SubmitTicket(**SUBMISSION))

        queue = run.subscribe()
        received = []
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=1)
            if event is None:
                break
            received.append(event.kind)
        assert received == [e.kind for e in run.events]

    async def test_a_live_subscriber_sees_events_as_they_happen(self, deps: Any) -> None:
        run = deps.store.create(
            ticket_id="tkt_api1", tenant_id="acme-payments", merchant_id="M-1001"
        )
        queue = run.subscribe()

        task = asyncio.create_task(execute(run, deps, SubmitTicket(**SUBMISSION)))
        seen = []
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=5)
            if event is None:
                break
            seen.append(event.kind)
        await task

        assert "run.started" in seen
        assert seen.count("node.completed") == 5

    async def test_a_disconnecting_subscriber_does_not_stall_the_run(self, deps: Any) -> None:
        run = deps.store.create(
            ticket_id="tkt_api1", tenant_id="acme-payments", merchant_id="M-1001"
        )
        queue = run.subscribe()
        run.unsubscribe(queue)

        await execute(run, deps, SubmitTicket(**SUBMISSION))
        assert run.status is RunStatus.COMPLETED


class TestEndpoints:
    def test_health_and_run_lookup(self, deps: Any) -> None:
        app = create_app(deps=deps)
        with TestClient(app) as client:
            assert client.get("/health").json() == {"status": "ok"}

            submitted = client.post("/tickets", json=SUBMISSION)
            assert submitted.status_code == 202
            run_id = submitted.json()["run_id"]

            # The run is in flight; polling must work regardless of its state.
            fetched = client.get(f"/runs/{run_id}")
            assert fetched.status_code == 200
            assert fetched.json()["run_id"] == run_id

            missing = client.get("/runs/run_999999")
            assert missing.status_code == 400
            assert missing.headers["content-type"].startswith("application/problem+json")

    def test_the_steps_endpoint_exposes_the_ledger(self, deps: Any) -> None:
        app = create_app(deps=deps)
        with TestClient(app) as client:
            run_id = client.post("/tickets", json=SUBMISSION).json()["run_id"]
            for _ in range(50):
                if client.get(f"/runs/{run_id}").json()["status"] == "completed":
                    break
            steps = client.get(f"/runs/{run_id}/steps").json()

        assert steps["run_id"] == run_id
        assert set(steps["scopes_used"]) <= {"crm:merchant:read", "knowledge:search"}

    def test_the_stream_endpoint_emits_sse(self, deps: Any) -> None:
        app = create_app(deps=deps)
        with TestClient(app) as client:
            run_id = client.post("/tickets", json=SUBMISSION).json()["run_id"]
            with client.stream("GET", f"/runs/{run_id}/stream") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                payloads = []
                for line in response.iter_lines():
                    if line.startswith("data: ") and line != "data: {}":
                        payloads.append(json.loads(line[6:]))
                    if line.startswith("event: done"):
                        break

        assert payloads, "the stream must deliver at least one event"
        assert payloads[0]["kind"] == "run.started"

    def test_a_malformed_submission_is_rejected(self, deps: Any) -> None:
        app = create_app(deps=deps)
        with TestClient(app) as client:
            assert (
                client.post("/tickets", json={**SUBMISSION, "merchant_id": "nope"}).status_code
                == 422
            )
            assert client.post("/tickets", json={**SUBMISSION, "escalate": True}).status_code == 422
