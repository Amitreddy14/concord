"""Supervisor routing, the specialists, and S02, S07 and S11 end to end.

Phase 5's definition of done. The scenarios run through the whole graph with
a scripted gateway and real mock services, so what is being tested is the
topology and the specialists rather than the model's prose.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from apps.mocks.faults import FaultConfig, FaultProfile
from apps.mocks.serve import build
from evals.fixtures import Scenario, reset_payload
from packages.agent.graph import build_graph, initial_state
from packages.agent.nodes import NodeContext
from packages.agent.specialists import ROUTES, _duplicate_captures, route
from packages.agent.verification import VerificationVerdict, verify_refund_proposal
from packages.policy.tenant import TenantConfig, load_tenant
from packages.shared.ledger import InMemoryLedger
from packages.shared.llm import ScriptedGateway
from packages.tools.invoker import ToolInvoker
from packages.tools.tests.conftest import BASE_URLS, RoutingTransport


def triage_json(summary: str, intent: str, **extra: Any) -> str:
    return json.dumps(
        {
            "intent": intent,
            "order_ref": extra.get("order_ref"),
            "mentions_amount": extra.get("mentions_amount", False),
            "urgency_claimed": extra.get("urgency_claimed", False),
            "suspected_injection": extra.get("suspected_injection", False),
            "summary": summary,
        }
    )


def draft_json(reply: str, citations: list[str]) -> str:
    return json.dumps({"reply": reply, "citations": citations, "unsupported_claims": []})


@pytest.fixture
def tenant() -> TenantConfig:
    load_tenant.cache_clear()
    return load_tenant("acme-payments")


async def run_graph(
    scenario: Scenario,
    tenant: TenantConfig,
    *,
    triage: str,
    draft: str = "",
    merchant_id: str,
    body: str | None = None,
) -> tuple[dict[str, Any], ToolInvoker]:
    services = {n: build(n, FaultConfig(profile=FaultProfile.OFF)) for n in BASE_URLS}
    payload = reset_payload(scenario)
    for app in services.values():
        with TestClient(app) as client:
            client.post("/_test/reset", json=payload)

    hosts = {
        httpx.URL(u).host: a for u, a in zip(BASE_URLS.values(), services.values(), strict=True)
    }
    async with httpx.AsyncClient(transport=RoutingTransport(hosts)) as client:
        invoker = ToolInvoker(
            tenant=tenant,
            base_urls=dict(BASE_URLS),
            client=client,
            ledger=InMemoryLedger(),
        )
        gateway = ScriptedGateway(
            responses={"triage": [triage], **({"draft": [draft]} if draft else {})}
        )
        context = NodeContext(
            gateway=gateway,
            invoker=invoker,
            tenant_id="acme-payments",
            require_citations=tenant.requires_citations,
            min_passage_score=tenant.grounding.get("min_passage_score"),
        )
        graph = build_graph(context, MemorySaver())
        state = initial_state(
            run_id=f"run_{scenario.scenario_id}",
            ticket_id=f"tkt_{scenario.scenario_id}",
            tenant_id="acme-payments",
            merchant_id=merchant_id,
            subject=scenario.ticket.get("subject", ""),
            body=body if body is not None else scenario.ticket.get("body", ""),
        )
        final: dict[str, Any] = await graph.ainvoke(
            state, config={"configurable": {"thread_id": scenario.scenario_id}}
        )
        return final, invoker


@asynccontextmanager
async def seeded_invoker(scenario: Scenario, tenant: TenantConfig) -> AsyncIterator[ToolInvoker]:
    """An invoker against services seeded for one scenario.

    A context manager because the httpx client must outlive the calls made
    through it — the first version of this helper closed the client on return
    and every later call failed with "client has been closed", which is a
    lifetime bug in the test rather than in the code under test.
    """
    services = {n: build(n, FaultConfig(profile=FaultProfile.OFF)) for n in BASE_URLS}
    payload = reset_payload(scenario)
    for app in services.values():
        with TestClient(app) as client:
            client.post("/_test/reset", json=payload)

    hosts = {
        httpx.URL(u).host: a for u, a in zip(BASE_URLS.values(), services.values(), strict=True)
    }
    async with httpx.AsyncClient(transport=RoutingTransport(hosts)) as client:
        yield ToolInvoker(
            tenant=tenant,
            base_urls=dict(BASE_URLS),
            client=client,
            ledger=InMemoryLedger(),
        )


class TestSupervisorRouting:
    @pytest.mark.parametrize("intent,specialist", sorted(ROUTES.items()))
    def test_every_intent_routes_somewhere(self, intent: str, specialist: str) -> None:
        assert route({"triage": {"intent": intent}}) == specialist  # type: ignore[arg-type]

    def test_an_unrecognised_intent_falls_back_rather_than_crashing(self) -> None:
        assert route({"triage": {"intent": "something_new"}}) == "knowledge_only"  # type: ignore[arg-type]

    def test_the_route_table_covers_the_triage_schema(self) -> None:
        # A new intent added to the schema without a route would silently fall
        # through to knowledge_only and read like a retrieval bug.
        from packages.agent.state import Intent

        assert set(ROUTES) == set(Intent.__args__)  # type: ignore[attr-defined]


class TestDuplicateCaptureDetection:
    def test_a_shared_gateway_reference_marks_the_later_capture(self) -> None:
        payment = {
            "captures": [
                {
                    "capture_id": "cap_1",
                    "gateway_reference": "GW-1",
                    "captured_at": "2026-10-14T11:02:11Z",
                },
                {
                    "capture_id": "cap_2",
                    "gateway_reference": "GW-1",
                    "captured_at": "2026-10-14T11:02:15Z",
                },
            ]
        }
        assert _duplicate_captures(payment) == ["cap_2"]

    def test_distinct_references_are_not_duplicates(self) -> None:
        # Two deliberate charges that happen to match in amount are not a
        # retry artifact, and refunding one would be taking money back that
        # the buyer meant to pay.
        payment = {
            "captures": [
                {
                    "capture_id": "cap_1",
                    "gateway_reference": "GW-1",
                    "captured_at": "2026-10-14T11:00:00Z",
                },
                {
                    "capture_id": "cap_2",
                    "gateway_reference": "GW-2",
                    "captured_at": "2026-10-14T11:05:00Z",
                },
            ]
        }
        assert _duplicate_captures(payment) == []


class TestScenario02:
    async def test_the_duplicate_capture_is_identified(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        final, _ = await run_graph(
            scenarios["S02"],
            tenant,
            triage=triage_json(
                "Merchant reports a duplicate charge on one order.",
                "refund_request",
                order_ref="8842",
            ),
            draft=draft_json(
                "The second capture was a retry and has been identified for reversal.",
                ["psg_refund-policy.v3.duplicate-charges"],
            ),
            merchant_id="M-1007",
        )

        assert "transactions" in final["visited"]
        assert final["resolution"]["outcome"] == "resolved"
        assert final["payment"]["payment_id"] == "pay_8842A"
        # The later of the two, not the first and not "the payment".
        assert final["duplicate_captures"] == ["cap_8842A2"]

    async def test_no_money_moves_in_phase_5(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        # Written in phase 5, when executing a refund was still ahead: the
        # specialist assembled facts and nothing moved money, so the
        # assertion was that nothing mutating was called at all.
        #
        # Phases 6 and 8 made that obsolete rather than wrong. S02's own
        # fixture asks for exactly one refund — the duplicate capture — so
        # the invariant worth asserting now is that the agent refunds that
        # one and touches nothing else about the account.
        _, invoker = await run_graph(
            scenarios["S02"],
            tenant,
            triage=triage_json("Duplicate charge.", "refund_request", order_ref="8842"),
            draft=draft_json("Identified.", ["psg_refund-policy.v3.duplicate-charges"]),
            merchant_id="M-1007",
        )
        from packages.tools import registry

        called = {s.tool for s in invoker.ledger.tool_calls("run_S02")}
        ticket_bookkeeping = {"set_ticket_status", "post_message", "assign_ticket", "flag_ticket"}
        permitted = {"create_refund"} | ticket_bookkeeping
        assert called.isdisjoint(set(registry.mutating_tools()) - permitted)

        # And nothing touched the merchant's account record.
        assert "add_merchant_note" not in called


class TestScenario07:
    async def test_the_settlement_and_incident_are_both_read(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        final, _ = await run_graph(
            scenarios["S07"],
            tenant,
            triage=triage_json(
                "Merchant reports a settlement still processing past its expected date.",
                "settlement_query",
            ),
            draft=draft_json(
                "The settlement is still processing and is waiting on the partner bank.",
                ["psg_settlement-policy.v2.delays"],
            ),
            merchant_id="M-1020",
        )

        assert "settlements" in final["visited"]
        assert final["settlements"][0]["settlement_id"] == "setl_3310"
        assert final["settlements"][0]["status"] == "processing"
        assert [i["incident_id"] for i in final["incidents"]] == ["inc_2208"]

    async def test_the_incident_carries_no_committed_resolution_date(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        # S07's hard assertion lives here: the fact the drafter is given must
        # not contain a date, because a date in the facts is a date in the
        # reply.
        final, _ = await run_graph(
            scenarios["S07"],
            tenant,
            triage=triage_json("Settlement delayed.", "settlement_query"),
            draft=draft_json("Still processing.", ["psg_settlement-policy.v2.delays"]),
            merchant_id="M-1020",
        )
        assert final["incidents"][0]["expected_resolution_at"] is None


class TestScenario11:
    async def test_an_ambiguous_reference_asks_instead_of_choosing(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        final, _ = await run_graph(
            scenarios["S11"],
            tenant,
            triage=triage_json(
                "Merchant reports a problem with an order and asks for a refund.",
                "refund_request",
                order_ref="8842",
            ),
            merchant_id="M-1052",
        )

        assert final["resolution"]["outcome"] == "ambiguous"
        assert final["reply"]["is_question"] is True
        assert final["status"] == "awaiting_customer"
        # The run stops at the specialist: no drafting against an unresolved
        # payment, which is where an invented answer would come from.
        assert "draft" not in final["visited"]

    async def test_both_candidates_appear_in_the_run(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        final, _ = await run_graph(
            scenarios["S11"],
            tenant,
            triage=triage_json("Problem with order 8842.", "refund_request", order_ref="8842"),
            merchant_id="M-1052",
        )
        considered = {c["payment_id"] for c in final["resolution"]["candidates"]}
        assert considered == {"pay_8842H", "pay_8842J"}

    async def test_no_refund_is_created(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        _, invoker = await run_graph(
            scenarios["S11"],
            tenant,
            triage=triage_json("Problem with order 8842.", "refund_request", order_ref="8842"),
            merchant_id="M-1052",
        )
        assert not invoker.ledger.tool_calls("run_S11", "create_refund")


class TestProposalVerification:
    async def test_a_sound_proposal_is_confirmed(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        async with seeded_invoker(scenarios["S02"], tenant) as invoker:
            result = await verify_refund_proposal(
                {"capture_id": "cap_8842A2", "amount": {"amount_minor": 245000, "currency": "INR"}},
                invoker=invoker,
                run_id="run_v1",
            )
        assert result.verdict is VerificationVerdict.CONFIRMED

    async def test_a_proposal_beyond_the_remainder_is_unsupported(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        async with seeded_invoker(scenarios["S02"], tenant) as invoker:
            result = await verify_refund_proposal(
                {
                    "capture_id": "cap_8842A2",
                    "amount": {"amount_minor": 99900000, "currency": "INR"},
                },
                invoker=invoker,
                run_id="run_v2",
            )
        assert result.verdict is VerificationVerdict.UNSUPPORTED

    async def test_a_changed_remainder_is_stale_not_merely_wrong(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        # The race the verifier exists for: a human refunded between the
        # specialist's read and the proposal. Named distinctly because
        # "someone else acted" is a different situation from "this proposal
        # was wrong to begin with".
        async with seeded_invoker(scenarios["S02"], tenant) as invoker:
            result = await verify_refund_proposal(
                {
                    "capture_id": "cap_8842A2",
                    "amount": {"amount_minor": 100000, "currency": "INR"},
                    "observed_remainder": {"amount_minor": 100000, "currency": "INR"},
                },
                invoker=invoker,
                run_id="run_v3",
            )
        assert result.verdict is VerificationVerdict.STALE

    async def test_an_unreadable_capture_is_unverifiable_not_permitted(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        # An action whose preconditions could not be confirmed must not
        # proceed on the grounds that nothing said no.
        async with seeded_invoker(scenarios["S02"], tenant) as invoker:
            result = await verify_refund_proposal(
                {"capture_id": "cap_missing", "amount": {"amount_minor": 1, "currency": "INR"}},
                invoker=invoker,
                run_id="run_v4",
            )
        assert result.verdict is VerificationVerdict.UNVERIFIABLE
        assert not result.confirmed
