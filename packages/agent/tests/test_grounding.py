"""Citation enforcement and the refusal path.

Phase 4's definition of done: S01, S05, S07 and S08 produce cited replies, and
a question with no supporting passage yields a refusal rather than a fluent
guess.
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
from evals.fixtures import Scenario, reset_payload
from packages.agent.graph import build_graph, initial_state
from packages.agent.grounding import GroundingVerdict, refusal_reply, verify
from packages.agent.nodes import NodeContext
from packages.policy.tenant import TenantConfig, load_tenant
from packages.shared.ledger import InMemoryLedger
from packages.shared.llm import ScriptedGateway
from packages.tools.invoker import ToolInvoker
from packages.tools.tests.conftest import BASE_URLS, RoutingTransport

RETRIEVED = [
    {"passage_id": "psg_a", "content_hash": "aaa"},
    {"passage_id": "psg_b", "content_hash": "bbb"},
]


def triage_json(summary: str, intent: str = "status_question") -> str:
    return json.dumps(
        {
            "intent": intent,
            "order_ref": None,
            "mentions_amount": False,
            "urgency_claimed": False,
            "suspected_injection": False,
            "summary": summary,
        }
    )


def draft_json(reply: str, citations: list[str], unsupported: list[str] | None = None) -> str:
    return json.dumps(
        {"reply": reply, "citations": citations, "unsupported_claims": unsupported or []}
    )


class TestVerifier:
    def test_a_supported_draft_may_be_sent(self) -> None:
        result = verify(
            draft={"citations": ["psg_a"], "unsupported_claims": []},
            retrieved=RETRIEVED,
            require_citations=True,
        )
        assert result.verdict is GroundingVerdict.SUPPORTED
        assert result.may_send

    def test_an_empty_retrieval_is_a_refusal(self) -> None:
        result = verify(draft={"citations": []}, retrieved=[], require_citations=True)
        assert result.verdict is GroundingVerdict.UNSUPPORTED
        assert not result.may_send

    def test_a_fabricated_citation_blocks_the_send(self) -> None:
        # The clearest signal of invention: an id that looks plausible for a
        # corpus the model did not see.
        result = verify(
            draft={"citations": ["psg_refund-policy.v9.invented"]},
            retrieved=RETRIEVED,
            require_citations=True,
        )
        assert result.verdict is GroundingVerdict.INVALID_CITATION
        assert not result.may_send

    def test_a_stale_citation_blocks_the_send(self) -> None:
        # The id is structural and survives rewording, which is right for
        # stability — but it means an id alone no longer proves the text is
        # what the drafter saw. The hash closes that gap.
        result = verify(
            draft={"citations": ["psg_a"], "citation_hashes": {"psg_a": "was-different"}},
            retrieved=RETRIEVED,
            require_citations=True,
        )
        assert result.verdict is GroundingVerdict.INVALID_CITATION

    def test_the_drafter_s_own_doubts_block_the_send(self) -> None:
        result = verify(
            draft={"citations": ["psg_a"], "unsupported_claims": ["refunds are instant"]},
            retrieved=RETRIEVED,
            require_citations=True,
        )
        assert result.verdict is GroundingVerdict.PARTIAL
        assert not result.may_send

    def test_a_tenant_requiring_citations_rejects_an_uncited_reply(self) -> None:
        assert not verify(
            draft={"citations": []}, retrieved=RETRIEVED, require_citations=True
        ).may_send
        assert verify(
            draft={"citations": []}, retrieved=RETRIEVED, require_citations=False
        ).may_send

    def test_a_refusal_does_not_explain_the_machinery(self) -> None:
        # A merchant does not need to know retrieval returned nothing. They
        # need to know a person is picking it up.
        text = refusal_reply(verify(draft={}, retrieved=[], require_citations=True))
        for leak in ("retriev", "corpus", "passage", "embedding", "index"):
            assert leak not in text.lower()


async def run_scenario(
    scenario: Scenario,
    tenant: TenantConfig,
    *,
    summary: str,
    draft: str,
    body: str,
    merchant_id: str,
) -> dict[str, Any]:
    services = {n: build(n, FaultConfig(profile=FaultProfile.OFF)) for n in BASE_URLS}
    payload = reset_payload(scenario)
    for app in services.values():
        with TestClient(app) as client:
            client.post("/_test/reset", json=payload)

    hosts = {
        httpx.URL(u).host: a for u, a in zip(BASE_URLS.values(), services.values(), strict=False)
    }
    async with httpx.AsyncClient(transport=RoutingTransport(hosts)) as client:
        gateway = ScriptedGateway(responses={"triage": [triage_json(summary)], "draft": [draft]})
        context = NodeContext(
            gateway=gateway,
            invoker=ToolInvoker(
                tenant=tenant,
                base_urls=dict(BASE_URLS),
                client=client,
                ledger=InMemoryLedger(),
            ),
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
            body=body,
        )
        result: dict[str, Any] = await graph.ainvoke(
            state, config={"configurable": {"thread_id": scenario.scenario_id}}
        )
        return result


@pytest.fixture
def tenant() -> TenantConfig:
    load_tenant.cache_clear()
    return load_tenant("acme-payments")


class TestGroundedScenarios:
    async def test_s01_answers_from_policy_with_a_citation(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        final = await run_scenario(
            scenarios["S01"],
            tenant,
            summary="Merchant asks how many working days until a card refund is credited.",
            draft=draft_json(
                "Card refunds reach the cardholder's statement in 5 to 7 working days.",
                ["psg_refund-policy.v3.credit-timelines"],
            ),
            body=scenarios["S01"].ticket["body"],
            merchant_id="M-1001",
        )
        assert final["grounding"]["verdict"] == "supported"
        assert final["reply"]["is_refusal"] is False
        assert final["reply"]["citations"] == ["psg_refund-policy.v3.credit-timelines"]

    async def test_s05_can_cite_the_window_rule_it_refuses_on(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        # The policy engine refuses the refund in phase 6; retrieval's job is
        # to make the rule citable, so the refusal can say why.
        final = await run_scenario(
            scenarios["S05"],
            tenant,
            summary="Merchant asks how many days after capture a refund can be initiated.",
            draft=draft_json(
                "A refund can be initiated within 180 days of the original capture.",
                ["psg_refund-policy.v3.eligibility-window"],
            ),
            body=scenarios["S05"].ticket["body"],
            merchant_id="M-1003",
        )
        assert final["grounding"]["verdict"] == "supported"
        assert "psg_refund-policy.v3.eligibility-window" in final["reply"]["citations"]

    async def test_s07_cites_settlement_policy(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        final = await run_scenario(
            scenarios["S07"],
            tenant,
            summary="Merchant reports a settlement still processing past its expected date.",
            draft=draft_json(
                "The settlement is still processing and is waiting on the partner bank.",
                ["psg_settlement-policy.v2.delays"],
            ),
            body=scenarios["S07"].ticket["body"],
            merchant_id="M-1020",
        )
        assert final["grounding"]["verdict"] == "supported"
        assert final["reply"]["citations"] == ["psg_settlement-policy.v2.delays"]

    async def test_s08_cites_the_limits_on_what_support_can_do(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        final = await run_scenario(
            scenarios["S08"],
            tenant,
            summary="Merchant asks why payouts are blocked and what support can do.",
            draft=draft_json(
                "Re-upload your registration certificate and a current bank statement; "
                "payouts resume automatically once verification completes.",
                ["psg_kyc-guide.v2.what-to-do-next"],
            ),
            body=scenarios["S08"].ticket["body"],
            merchant_id="M-1031",
        )
        assert final["grounding"]["verdict"] == "supported"
        assert "psg_kyc-guide.v2.what-to-do-next" in final["reply"]["citations"]


class TestRefusalPath:
    async def test_an_unsupported_question_refuses_without_drafting(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        # The graph skips the draft node entirely when retrieval is empty.
        # Drafting anyway would produce a fluent reply with nothing behind it,
        # which is the most expensive thing this system could do.
        final = await run_scenario(
            scenarios["S01"],
            tenant,
            summary="Merchant asks for a restaurant recommendation.",
            draft=draft_json("The best place is around the corner.", []),
            body="Can you recommend a good restaurant near our office?",
            merchant_id="M-1001",
        )
        assert final["visited"] == ["triage", "gather", "knowledge_only", "retrieve", "verify"]
        assert "draft" not in final["visited"]
        assert final["grounding"]["verdict"] == "unsupported"
        assert final["reply"]["is_refusal"] is True
        assert final["reply"]["citations"] == []

    async def test_a_fabricated_citation_is_caught_end_to_end(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        final = await run_scenario(
            scenarios["S01"],
            tenant,
            summary="Merchant asks how many working days until a card refund is credited.",
            draft=draft_json(
                "Refunds are credited instantly.", ["psg_refund-policy.v9.instant-refunds"]
            ),
            body=scenarios["S01"].ticket["body"],
            merchant_id="M-1001",
        )
        assert final["grounding"]["verdict"] == "invalid_citation"
        assert final["reply"]["is_refusal"] is True
        assert "instantly" not in final["reply"]["text"]

    async def test_the_run_always_produces_a_reply(
        self, scenarios: dict[str, Scenario], tenant: TenantConfig
    ) -> None:
        # A graph that can end with no reply leaves a merchant waiting, which
        # is worse than being told a person is picking it up.
        final = await run_scenario(
            scenarios["S01"],
            tenant,
            summary="Merchant asks about something outside the corpus entirely.",
            draft=draft_json("x", []),
            body="How do I train for a marathon?",
            merchant_id="M-1001",
        )
        assert final["reply"]["text"].strip()
