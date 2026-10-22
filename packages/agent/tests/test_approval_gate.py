"""The human approval gate: S03, S04 and its four branches, S05, S08.

The assertion this module exists for is S04's first one — *the ledger is
untouched for as long as the run is paused*. It is the assertion that catches a
mutation placed before the interrupt, and it cannot be replaced by reading the
code, because the defect it catches looks correct in the code: a node that
creates the refund and then asks for approval reads as "do the work, confirm
it", and produces two refunds the moment LangGraph re-executes it on resume.

So these tests count refunds in the payments service, not calls in a mock.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from apps.mocks.faults import FaultConfig, FaultProfile
from apps.mocks.serve import build
from evals.fixtures import Scenario, load_all, reset_payload
from packages.agent.context import NodeContext
from packages.agent.graph import build_graph, initial_state
from packages.policy.approval import (
    ApprovalStatus,
    InMemoryApprovalStore,
)
from packages.policy.tenant import TenantConfig, load_tenant
from packages.shared.ledger import InMemoryLedger
from packages.shared.llm import ScriptedGateway
from packages.tools.invoker import ToolInvoker
from packages.tools.tests.conftest import BASE_URLS, RoutingTransport


def _triage(intent: str = "refund_request", order_ref: str | None = None) -> str:
    return json.dumps(
        {
            "intent": intent,
            "order_ref": order_ref,
            "mentions_amount": False,
            "urgency_claimed": False,
            "suspected_injection": False,
            "summary": "Merchant asks for a refund.",
        }
    )


_DRAFT = json.dumps(
    {
        "reply": "That refund is on its way; it reaches the cardholder in 5 to 7 working days.",
        "citations": ["psg_refund-policy.v3.credit-timelines"],
        "unsupported_claims": [],
    }
)


@pytest.fixture(scope="session")
def all_scenarios() -> dict[str, Scenario]:
    return load_all()


@pytest.fixture
def tenant() -> TenantConfig:
    load_tenant.cache_clear()
    return load_tenant("acme-payments")


class Rig:
    """A graph, the services behind it, and the approval index beside it."""

    def __init__(
        self,
        context: NodeContext,
        apps: dict[str, Any],
        saver: MemorySaver,
        scenario: Scenario,
    ) -> None:
        self.context = context
        self.apps = apps
        self.saver = saver
        self.scenario = scenario
        self.graph = build_graph(context, saver)

    @property
    def approvals(self) -> InMemoryApprovalStore:
        store = self.context.approvals
        assert isinstance(store, InMemoryApprovalStore)
        return store

    def config(self, ticket_id: str = "tkt_s04") -> dict[str, Any]:
        return {"configurable": {"thread_id": f"ticket:{ticket_id}"}}

    def state(self, ticket_id: str = "tkt_s04") -> Any:
        ticket = self.scenario.ticket
        return initial_state(
            run_id=f"run_{ticket_id}",
            ticket_id=ticket_id,
            tenant_id="acme-payments",
            merchant_id=str(ticket["merchant_id"]),
            subject=str(ticket["subject"]),
            body=str(ticket["body"]),
        )

    def refunds(self) -> list[dict[str, Any]]:
        """Every refund the payments service holds. The ledger of record.

        Read from the service's own store rather than counted from the
        invoker's calls. A test that counted calls would pass while the
        service created two refunds from one retried call, which is the
        failure S09 exists for and this assertion must not be blind to.
        """
        return list(self.apps["payments"].state.store.refunds.values())

    def restart(self) -> None:
        """Rebuild the graph against the same checkpointer.

        As close to a process restart as a test gets without forking: a new
        graph object, the same durable state. The resume has to come from the
        checkpoint, because this object has never seen the run.
        """
        self.graph = build_graph(self.context, self.saver)


@pytest.fixture
async def rig(
    request: pytest.FixtureRequest,
    all_scenarios: dict[str, Scenario],
    tenant: TenantConfig,
) -> AsyncIterator[Rig]:
    scenario_id = getattr(request, "param", "S04")
    scenario = all_scenarios[scenario_id]

    apps = {n: build(n, FaultConfig(profile=FaultProfile.OFF)) for n in BASE_URLS}
    payload = reset_payload(scenario)
    for app in apps.values():
        with TestClient(app) as client:
            assert client.post("/_test/reset", json=payload).status_code == 200

    gateway = ScriptedGateway(responses={"triage": [_triage()] * 4, "draft": [_DRAFT] * 4})
    hosts = {httpx.URL(u).host: a for u, a in zip(BASE_URLS.values(), apps.values(), strict=False)}
    async with httpx.AsyncClient(transport=RoutingTransport(hosts)) as client:
        context = NodeContext(
            gateway=gateway,
            invoker=ToolInvoker(
                tenant=tenant,
                base_urls=dict(BASE_URLS),
                client=client,
                ledger=InMemoryLedger(),
            ),
            tenant_id="acme-payments",
            approvals=InMemoryApprovalStore(),
        )
        yield Rig(context, apps, MemorySaver(), scenario)


# ---------------------------------------------------------------------------
# S04: above the cap, so a human decides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rig", ["S04"], indirect=True)
class TestS04Pause:
    async def test_the_run_pauses_and_the_ledger_is_untouched(self, rig: Rig) -> None:
        config = rig.config()
        async for _ in rig.graph.astream(rig.state(), config=config):
            pass

        snapshot = await rig.graph.aget_state(config)
        assert snapshot.next, "the run should be paused, not finished"

        # The assertion S04 exists for. A refund here means a mutation ran
        # before the interrupt, and would run again on resume.
        assert rig.refunds() == []
        assert snapshot.values["status"] == "awaiting_approval"

    async def test_the_approval_record_carries_what_a_reviewer_needs(self, rig: Rig) -> None:
        async for _ in rig.graph.astream(rig.state(), config=rig.config()):
            pass

        pending = rig.approvals.pending(tenant_id="acme-payments")
        assert len(pending) == 1
        row = pending[0].to_review_payload()

        for field in ("proposed_action", "risk_tier", "required_scope", "expires_at"):
            assert row[field] is not None, f"approval record is missing {field}"
        assert row["risk_tier"] == "R2-elevated-refund"
        assert row["required_scope"] == "payments:refund:write"

    async def test_the_interrupt_payload_shows_evidence_not_the_ticket_body(self, rig: Rig) -> None:
        config = rig.config()
        async for _ in rig.graph.astream(rig.state(), config=config):
            pass

        snapshot = await rig.graph.aget_state(config)
        payload = snapshot.tasks[0].interrupts[0].value
        assert payload["kind"] == "approval_required"
        assert payload["evidence"]["captures"], "a reviewer needs the captures"

        # ADR 0005 rule 4: the panel that authorises money carries ledger
        # facts, never merchant-supplied text.
        body = rig.scenario.ticket["body"]
        assert body not in json.dumps(payload)

    async def test_a_restart_while_pending_resumes_from_the_checkpoint(self, rig: Rig) -> None:
        config = rig.config()
        async for _ in rig.graph.astream(rig.state(), config=config):
            pass
        before = await rig.graph.aget_state(config)
        approval_id = rig.approvals.pending()[0].approval_id

        rig.restart()

        after = await rig.graph.aget_state(config)
        assert after.values["visited"] == before.values["visited"]
        assert after.values["proposal"] == before.values["proposal"]

        rig.approvals.decide(approval_id, status=ApprovalStatus.APPROVED, reviewer="dana@acme")
        async for _ in rig.graph.astream(
            Command(resume={"approval_id": approval_id}), config=config
        ):
            pass

        assert len(rig.refunds()) == 1


@pytest.mark.parametrize("rig", ["S04"], indirect=True)
class TestS04Branches:
    async def _pause(self, rig: Rig) -> tuple[dict[str, Any], str]:
        config = rig.config()
        async for _ in rig.graph.astream(rig.state(), config=config):
            pass
        return config, rig.approvals.pending()[0].approval_id

    async def _resume(self, rig: Rig, config: dict[str, Any], approval_id: str) -> Any:
        async for _ in rig.graph.astream(
            Command(resume={"approval_id": approval_id}), config=config
        ):
            pass
        return await rig.graph.aget_state(config)

    async def test_approved_refunds_the_full_amount_and_cites_the_approval(self, rig: Rig) -> None:
        config, approval_id = await self._pause(rig)
        rig.approvals.decide(approval_id, status=ApprovalStatus.APPROVED, reviewer="dana@acme")
        await self._resume(rig, config, approval_id)

        refunds = rig.refunds()
        assert len(refunds) == 1
        assert refunds[0]["amount"] == {"amount_minor": 4120000, "currency": "INR"}
        assert refunds[0]["approval_id"] == approval_id
        assert refunds[0]["initiated_by"] == "human"

    async def test_rejected_moves_no_money(self, rig: Rig) -> None:
        config, approval_id = await self._pause(rig)
        rig.approvals.decide(
            approval_id,
            status=ApprovalStatus.REJECTED,
            reviewer="dana@acme",
            note="merchant already disputed this through the issuer",
        )
        snapshot = await self._resume(rig, config, approval_id)

        assert rig.refunds() == []
        assert snapshot.values["approval"]["reviewer_note"]

    async def test_modified_refunds_the_reviewers_amount_and_keeps_both(self, rig: Rig) -> None:
        config, approval_id = await self._pause(rig)
        original = rig.approvals.get(approval_id)
        assert original is not None
        half = {
            **original.proposal,
            "amount": {"amount_minor": 2060000, "currency": "INR"},
        }
        rig.approvals.decide(
            approval_id,
            status=ApprovalStatus.MODIFIED,
            reviewer="dana@acme",
            note="refunding half; the merchant kept one unit",
            modified_proposal=half,
        )
        await self._resume(rig, config, approval_id)

        refunds = rig.refunds()
        assert len(refunds) == 1
        assert refunds[0]["amount"] == {"amount_minor": 2060000, "currency": "INR"}

        # Both the ask and the authorisation survive. A record that kept only
        # the reviewer's figure could not answer "what did the agent want?"
        row = rig.approvals.get(approval_id)
        assert row is not None
        assert row.proposal["amount"] == {"amount_minor": 4120000, "currency": "INR"}
        assert row.modified_proposal == half

    async def test_expired_moves_no_money_and_fails_the_run(self, rig: Rig) -> None:
        config, approval_id = await self._pause(rig)

        # The sweep LangGraph will not do: a frozen thread never expires
        # itself, so time passing has to be acted on from outside.
        swept = rig.approvals.sweep_expired(now=datetime.now(UTC) + timedelta(hours=49))
        assert [r.approval_id for r in swept] == [approval_id]

        snapshot = await self._resume(rig, config, approval_id)
        assert rig.refunds() == []
        assert snapshot.values["status"] == "failed"
        assert snapshot.values["failure"] == "approval_expired"

    async def test_a_decided_request_is_not_reopened(self, rig: Rig) -> None:
        _, approval_id = await self._pause(rig)
        rig.approvals.decide(approval_id, status=ApprovalStatus.APPROVED, reviewer="dana@acme")

        from packages.policy.approval import ApprovalError

        with pytest.raises(ApprovalError, match="already"):
            rig.approvals.decide(
                approval_id, status=ApprovalStatus.REJECTED, reviewer="someone-else"
            )

    async def test_the_sweep_leaves_a_decided_request_alone(self, rig: Rig) -> None:
        _, approval_id = await self._pause(rig)
        rig.approvals.decide(approval_id, status=ApprovalStatus.APPROVED, reviewer="dana@acme")

        # The race is real: a reviewer clicks approve as the sweep runs.
        # Whoever got there first wins, and here that is the reviewer.
        assert rig.approvals.sweep_expired(now=datetime.now(UTC) + timedelta(days=9)) == []
        row = rig.approvals.get(approval_id)
        assert row is not None and row.status is ApprovalStatus.APPROVED


# ---------------------------------------------------------------------------
# S03: within the cap, so no human is involved at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rig", ["S03"], indirect=True)
class TestS03Autonomous:
    async def test_a_refund_within_the_cap_needs_no_approval(self, rig: Rig) -> None:
        config = rig.config("tkt_s03")
        async for _ in rig.graph.astream(rig.state("tkt_s03"), config=config):
            pass
        snapshot = await rig.graph.aget_state(config)

        assert not snapshot.next, "an autonomous refund should not pause"
        assert rig.approvals.pending() == [], "no approval record for an autonomous action"

        refunds = rig.refunds()
        assert len(refunds) == 1
        assert refunds[0]["capture_id"] == "cap_9120B1"
        assert refunds[0]["amount"] == {"amount_minor": 89000, "currency": "INR"}
        assert refunds[0]["reason_code"] == "buyer_request"
        assert refunds[0]["initiated_by"] == "agent"
        assert snapshot.values["decision"]["risk_tier"] == "R1-routine-refund"


# ---------------------------------------------------------------------------
# S05: refused by the rules, so no approval is offered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rig", ["S05"], indirect=True)
class TestS05OutsideWindow:
    async def test_the_refusal_happens_in_the_policy_layer(self, rig: Rig) -> None:
        config = rig.config("tkt_s05")
        async for _ in rig.graph.astream(rig.state("tkt_s05"), config=config):
            pass
        snapshot = await rig.graph.aget_state(config)
        decision = snapshot.values["decision"]

        assert decision["permission"] == "refused"
        assert decision["rule_id"] == "W0-outside-refund-window"

        # S05's own words: "a 422 from the payments service means the refusal
        # happened in the wrong layer." So the tool must never have been
        # called, not merely have failed.
        ledger = rig.context.invoker.ledger
        assert ledger.tool_calls("run_tkt_s05", tool="create_refund") == []
        assert rig.refunds() == []

        # Refused is not awaiting: offering an approval would imply a reviewer
        # could wave through a capture that is simply too old.
        assert rig.approvals.pending() == []
        assert "await_approval" not in snapshot.values["visited"]

    async def test_the_decision_records_the_inputs_it_was_evaluated_against(self, rig: Rig) -> None:
        config = rig.config("tkt_s05")
        async for _ in rig.graph.astream(rig.state("tkt_s05"), config=config):
            pass
        snapshot = await rig.graph.aget_state(config)
        inputs = snapshot.values["decision"]["inputs"]

        # S05 asks for rule_id and evaluated_inputs. Without the inputs, a
        # rule id is an assertion a reviewer has to take on trust.
        assert inputs["window_days"] == 180
        assert inputs["capture_age_days"] > 180
        assert snapshot.values["decision"]["citations"] == [
            "psg_refund-policy.v3.eligibility-window"
        ]


# ---------------------------------------------------------------------------
# S08: forbidden, so no approval is offered either
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rig", ["S08"], indirect=True)
class TestS08Forbidden:
    @staticmethod
    def _hold_query_gateway() -> ScriptedGateway:
        return ScriptedGateway(
            responses={
                "triage": [_triage(intent="payout_hold_query")],
                "draft": [
                    json.dumps(
                        {
                            "reply": (
                                "Your payouts are held pending a compliance review. "
                                "I can't change that from here, so I've routed this to "
                                "the compliance team."
                            ),
                            "citations": ["psg_payout-holds.v2.what-to-tell-merchants"],
                            "unsupported_claims": [],
                        }
                    )
                ],
            }
        )

    async def test_a_forbidden_action_is_classified_refused_and_never_attempted(
        self, rig: Rig
    ) -> None:
        rig.context.gateway = self._hold_query_gateway()
        rig.graph = build_graph(rig.context, rig.saver)

        config = rig.config("tkt_s08")
        async for _ in rig.graph.astream(rig.state("tkt_s08"), config=config):
            pass
        snapshot = await rig.graph.aget_state(config)
        decision = snapshot.values["decision"]

        assert decision["permission"] == "forbidden"
        assert decision["rule_id"] == "F0-forbidden-action"
        assert decision["risk_tier"] == "R3-compliance"
        assert decision["inputs"]["forbidden_action"] == "lift_payout_hold"

        # R3 is `forbidden`, not `reviewer`. Raising an approval request would
        # imply a human in this console could authorise it.
        assert rig.approvals.pending() == []
        assert "await_approval" not in snapshot.values["visited"]
        assert "act" not in snapshot.values["visited"]

    async def test_the_merchants_claimed_tier_does_not_reach_the_decision(self, rig: Rig) -> None:
        # S08's merchant asserts enterprise tier in the ticket. The CRM says
        # standard, and the CRM is the system of record (ADR 0005, rule 3).
        assert "enterprise" in rig.scenario.ticket["body"].lower()

        rig.context.gateway = self._hold_query_gateway()
        rig.graph = build_graph(rig.context, rig.saver)

        config = rig.config("tkt_s08")
        async for _ in rig.graph.astream(rig.state("tkt_s08"), config=config):
            pass
        snapshot = await rig.graph.aget_state(config)

        assert snapshot.values["decision"]["inputs"]["merchant_tier"] == "standard"
