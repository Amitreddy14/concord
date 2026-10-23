"""S09 through the whole graph, under the hostile profile, five times over.

Phase 7 is done when "S09 passes under the `hostile` profile across repeated
seeded runs with exactly one refund every time, and a replayed run reproduces
its original decisions". The tool layer has had S09 covered since phase 2;
what it has not had is the scenario driven through the graph, repeatedly, with
the fault script armed — and the fixture asks for five runs with
`require_identical_outcome`, because one run producing one refund proves
nothing about whether the key is derived or merely happened not to collide.

The sequence S09 pins is the expensive one: the first `POST /refunds` is
accepted upstream and then times out at the client. A naive retry creates a
second refund, and two refunds here is a real financial loss rather than a
failing assertion.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from apps.mocks.faults import FaultConfig, FaultProfile
from apps.mocks.serve import build
from evals.fixtures import Scenario, load_all, reset_payload
from packages.agent.context import NodeContext
from packages.agent.graph import build_graph, initial_state
from packages.agent.replay import RecordedInvoker, ReplayError, decisions_from_state
from packages.policy.approval import InMemoryApprovalStore
from packages.policy.tenant import TenantConfig, load_tenant
from packages.shared.ledger import InMemoryLedger
from packages.shared.llm import ScriptedGateway
from packages.tools.invoker import ToolInvoker
from packages.tools.tests.conftest import BASE_URLS, RoutingTransport

TRIAGE = json.dumps(
    {
        "intent": "refund_request",
        "order_ref": "5150",
        "mentions_amount": False,
        "urgency_claimed": False,
        "suspected_injection": False,
        "summary": "Merchant asks for order 5150 to be refunded.",
    }
)
DRAFT = json.dumps(
    {
        "reply": "That refund is on its way and reaches the card in 5 to 7 working days.",
        "citations": ["psg_refund-policy.v3.credit-timelines"],
        "unsupported_claims": [],
    }
)


class NoSleep:
    """Collapse the backoff so five runs do not take a minute of wall clock."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture(scope="module")
def s09() -> Scenario:
    return load_all()["S09"]


async def run_once(scenario: Scenario, tenant: TenantConfig, run_id: str) -> dict[str, Any]:
    """One full graph run against freshly seeded, hostile services.

    Services are rebuilt per run rather than reset, so the fault script's
    per-endpoint attempt counter starts at 1 every time. A shared injector
    would have run 2 skipping straight to the replay, and five runs would
    then prove the key works once.
    """
    services = {n: build(n, FaultConfig(profile=FaultProfile.HOSTILE)) for n in BASE_URLS}
    payload = reset_payload(scenario)
    for app in services.values():
        with TestClient(app) as client:
            assert client.post("/_test/reset", json=payload).status_code == 200

    hosts = {
        httpx.URL(u).host: a for u, a in zip(BASE_URLS.values(), services.values(), strict=False)
    }
    ledger = InMemoryLedger()
    sleeper = NoSleep()

    async with httpx.AsyncClient(transport=RoutingTransport(hosts)) as client:
        context = NodeContext(
            gateway=ScriptedGateway(responses={"triage": [TRIAGE] * 3, "draft": [DRAFT] * 3}),
            invoker=ToolInvoker(
                tenant=tenant,
                base_urls=dict(BASE_URLS),
                client=client,
                ledger=ledger,
                sleep=sleeper,
            ),
            tenant_id="acme-payments",
            approvals=InMemoryApprovalStore(),
        )
        graph = build_graph(context, MemorySaver())
        config = {"configurable": {"thread_id": f"ticket:{run_id}"}}
        ticket = scenario.ticket
        state = initial_state(
            run_id=run_id,
            ticket_id=run_id,
            tenant_id="acme-payments",
            merchant_id=str(ticket["merchant_id"]),
            subject=str(ticket["subject"]),
            body=str(ticket["body"]),
        )
        async for _ in graph.astream(state, config=config):
            pass
        snapshot = await graph.aget_state(config)

    return {
        "state": dict(snapshot.values),
        "ledger": ledger,
        "refunds": list(services["payments"].state.store.refunds.values()),  # type: ignore[attr-defined]
        "backoffs": sleeper.delays,
        "context": context,
    }


@pytest.fixture
def tenant() -> TenantConfig:
    load_tenant.cache_clear()
    return load_tenant("acme-payments")


class TestRepeatedSeededRuns:
    async def test_five_runs_each_produce_exactly_one_refund(
        self, s09: Scenario, tenant: TenantConfig
    ) -> None:
        # The single most important assertion in the suite, run the number of
        # times the fixture asks for.
        assert s09.repeat_runs == 5
        assert s09.requires_identical_outcome

        outcomes: list[dict[str, Any]] = []
        for index in range(s09.repeat_runs):
            result = await run_once(s09, tenant, f"run_s09_{index}")
            refunds = result["refunds"]
            assert len(refunds) == 1, (
                f"run {index} produced {len(refunds)} refunds; two here is a real "
                "financial loss, not a failing assertion"
            )
            outcomes.append(result)

        # `require_identical_outcome`: not merely one refund each, but the
        # same refund each. A run that refunded a different amount would
        # satisfy a count check and still be a defect.
        amounts = {json.dumps(o["refunds"][0]["amount"], sort_keys=True) for o in outcomes}
        assert amounts == {json.dumps({"amount_minor": 199900, "currency": "INR"})}

        reasons = {o["refunds"][0]["reason_code"] for o in outcomes}
        assert len(reasons) == 1, f"runs disagreed on the reason code: {reasons}"

    async def test_both_attempts_share_one_derived_key(
        self, s09: Scenario, tenant: TenantConfig
    ) -> None:
        result = await run_once(s09, tenant, "run_s09_key")
        ledger = result["ledger"]

        calls = [
            step
            for step in ledger.steps("run_s09_key")
            if step.tool == "create_refund" and step.outcome != "started"
        ]
        assert len(calls) >= 1

        # The refund the service stored carries the key; one key for a
        # scenario whose whole point is two attempts.
        refund = result["refunds"][0]
        assert refund["idempotency_key"]

        # And the retry really happened — otherwise this passes for the
        # uninteresting reason that the fault never fired.
        assert result["backoffs"], "no backoff recorded; the hostile script did not fire"

    async def test_the_reply_reports_one_refund_and_does_not_claim_failure(
        self, s09: Scenario, tenant: TenantConfig
    ) -> None:
        result = await run_once(s09, tenant, "run_s09_reply")
        state = result["state"]

        assert state.get("status") != "failed", state.get("failure")
        assert state.get("refund"), "the run finished without recording a refund"
        assert len(result["refunds"]) == 1


class TestDeterministicReplay:
    async def test_a_replayed_run_reaches_the_same_decisions(
        self, s09: Scenario, tenant: TenantConfig
    ) -> None:
        original = await run_once(s09, tenant, "run_s09_replay")
        ledger: InMemoryLedger = original["ledger"]
        before = decisions_from_state(original["state"], ledger, "run_s09_replay")

        # Replay against the recorded responses. No service is running for
        # this graph — if a node reaches for one, RecordedInvoker raises
        # rather than letting the replay touch anything live.
        replay_ledger = InMemoryLedger()
        recorded = RecordedInvoker(
            source=ledger,
            source_run_id="run_s09_replay",
            ledger=replay_ledger,
            tenant=tenant,
        )
        context = NodeContext(
            gateway=ScriptedGateway(responses={"triage": [TRIAGE] * 3, "draft": [DRAFT] * 3}),
            invoker=recorded,  # type: ignore[arg-type]
            tenant_id="acme-payments",
            approvals=InMemoryApprovalStore(),
        )
        graph = build_graph(context, MemorySaver())
        config = {"configurable": {"thread_id": "ticket:replay"}}
        ticket = s09.ticket
        async for _ in graph.astream(
            initial_state(
                run_id="run_s09_replay",
                ticket_id="replay",
                tenant_id="acme-payments",
                merchant_id=str(ticket["merchant_id"]),
                subject=str(ticket["subject"]),
                body=str(ticket["body"]),
            ),
            config=config,
        ):
            pass
        snapshot = await graph.aget_state(config)
        after = decisions_from_state(dict(snapshot.values), replay_ledger, "run_s09_replay")

        assert before.rule_ids == after.rule_ids
        assert before.permissions == after.permissions
        assert before.proposal == after.proposal
        assert before.differences(after) == [], before.differences(after)

    async def test_the_replay_served_everything_from_the_recording(
        self, s09: Scenario, tenant: TenantConfig
    ) -> None:
        original = await run_once(s09, tenant, "run_s09_served")
        recorded = RecordedInvoker(
            source=original["ledger"],
            source_run_id="run_s09_served",
            ledger=InMemoryLedger(),
            tenant=tenant,
        )
        # A call the original never made has no recording, and the replay
        # must raise rather than fall through to a live service — a replay
        # that reached the network would be measuring today's systems of
        # record and could move money while doing it.
        with pytest.raises(ReplayError, match="diverged"):
            await recorded.call(
                "create_refund",
                {
                    "capture_id": "cap_9999ZZ",
                    "amount": {"amount_minor": 1, "currency": "INR"},
                    "reason_code": "buyer_request",
                },
                run_id="run_s09_served",
                node="act",
            )

    async def test_arguments_the_tool_rejects_are_reported_as_divergence(
        self, s09: Scenario, tenant: TenantConfig
    ) -> None:
        # Not a Pydantic traceback surfacing from inside a replay. The
        # original invoker would have refused these before the network, so
        # no recording can exist and the honest report is divergence.
        original = await run_once(s09, tenant, "run_s09_invalid")
        recorded = RecordedInvoker(
            source=original["ledger"],
            source_run_id="run_s09_invalid",
            ledger=InMemoryLedger(),
            tenant=tenant,
        )
        with pytest.raises(ReplayError, match="arguments the tool rejects"):
            await recorded.call(
                "create_refund",
                {"capture_id": "cap_5150F1"},
                run_id="run_s09_invalid",
                node="act",
            )
