"""Rig for the agent tests.

The gateway is scripted, the services are real. That split is deliberate: the
model's output is the thing a test needs to control, and the systems of record
are the thing a test must not fake, or the graph passes here and fails against
anything real.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from apps.mocks.faults import FaultConfig, FaultProfile
from apps.mocks.serve import build
from evals.fixtures import Scenario, load_all, reset_payload
from packages.agent.nodes import NodeContext
from packages.policy.tenant import TenantConfig, load_tenant
from packages.shared.ledger import InMemoryLedger
from packages.shared.llm import ScriptedGateway
from packages.tools.invoker import ToolInvoker
from packages.tools.tests.conftest import BASE_URLS, RoutingTransport

S01_TRIAGE = json.dumps(
    {
        "intent": "status_question",
        "order_ref": None,
        "mentions_amount": False,
        "urgency_claimed": False,
        "suspected_injection": False,
        "summary": "Merchant asks how long refunds take to reach a customer.",
    }
)

S01_DRAFT = json.dumps(
    {
        "reply": (
            "Card refunds reach the cardholder's statement in 5 to 7 working days. "
            "UPI and wallet refunds are usually credited within 3 working days."
        ),
        "citations": ["psg_refund-policy.v3.s2"],
        "unsupported_claims": [],
    }
)


@pytest.fixture(scope="session")
def scenarios() -> dict[str, Scenario]:
    return load_all()


@pytest.fixture
def tenant() -> TenantConfig:
    load_tenant.cache_clear()
    return load_tenant("acme-payments")


@pytest.fixture
def services(scenarios: dict[str, Scenario]) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    apps = {n: build(n, FaultConfig(profile=FaultProfile.OFF)) for n in BASE_URLS}
    payload = reset_payload(scenarios["S01"])
    for app in apps.values():
        with TestClient(app) as client:
            assert client.post("/_test/reset", json=payload).status_code == 200
    return apps


@pytest.fixture
def gateway() -> ScriptedGateway:
    return ScriptedGateway(responses={"triage": [S01_TRIAGE], "draft": [S01_DRAFT]})


@pytest.fixture
async def context(
    tenant: TenantConfig, services: dict[str, Any], gateway: ScriptedGateway
) -> AsyncIterator[NodeContext]:
    hosts = {
        httpx.URL(u).host: a for u, a in zip(BASE_URLS.values(), services.values(), strict=False)
    }
    async with httpx.AsyncClient(transport=RoutingTransport(hosts)) as client:
        yield NodeContext(
            gateway=gateway,
            invoker=ToolInvoker(
                tenant=tenant,
                base_urls=dict(BASE_URLS),
                client=client,
                ledger=InMemoryLedger(),
            ),
            tenant_id="acme-payments",
        )
