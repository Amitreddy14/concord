"""The dead-letter endpoints, including the replay the API refuses.

The refusal is what these are really for. A letter whose effect upstream is
unknown must not be replayed, and the endpoint has to say so with a status
that tells a console what to do rather than replaying behind a warning.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.main import Dependencies, create_app
from apps.api.runs import RunStore
from apps.mocks.faults import FaultConfig, FaultProfile
from apps.mocks.serve import build
from evals.fixtures import load_all, reset_payload
from packages.agent.deadletter import (
    InMemoryDeadLetterQueue,
    LetterStatus,
    from_failed_run,
)
from packages.policy.approval import InMemoryApprovalStore
from packages.policy.tenant import load_tenant
from packages.shared.ledger import InMemoryLedger
from packages.shared.llm import ScriptedGateway
from packages.tools.reconcile import Resolution
from packages.tools.tests.conftest import BASE_URLS, RoutingTransport


@pytest.fixture
def deps() -> Iterator[Dependencies]:
    scenario = load_all()["S03"]
    services = {n: build(n, FaultConfig(profile=FaultProfile.OFF)) for n in BASE_URLS}
    payload = reset_payload(scenario)
    for app in services.values():
        with TestClient(app) as client:
            assert client.post("/_test/reset", json=payload).status_code == 200

    hosts = {
        httpx.URL(u).host: a for u, a in zip(BASE_URLS.values(), services.values(), strict=False)
    }
    load_tenant.cache_clear()
    yield Dependencies(
        tenant=load_tenant("acme-payments"),
        gateway=ScriptedGateway(responses={}),
        base_urls=dict(BASE_URLS),
        client=httpx.AsyncClient(transport=RoutingTransport(hosts)),
        ledger=InMemoryLedger(),
        store=RunStore(),
        approvals=InMemoryApprovalStore(),
        dead_letters=InMemoryDeadLetterQueue(),
    )


@pytest.fixture
def client(deps: Dependencies) -> Iterator[TestClient]:
    with TestClient(create_app(deps)) as test_client:
        yield test_client


def file_letter(
    deps: Dependencies,
    *,
    run_id: str = "run_000001",
    failure: str = "refund_failed_without_effect",
    resolution: str = str(Resolution.ABSENT),
) -> Any:
    return deps.dead_letters.add(
        from_failed_run(
            run_id=run_id,
            ticket_id="tkt_1",
            tenant_id="acme-payments",
            merchant_id="M-1007",
            failure=failure,
            state={
                "triage": {"intent": "refund_request"},
                "proposal": {"capture_id": "cap_9120B1"},
                "reconciliation": {"resolution": resolution},
            },
        )
    )


class TestTheQueue:
    def test_open_letters_are_listed_with_what_needs_a_person(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        file_letter(deps, run_id="run_clean")
        file_letter(
            deps,
            run_id="run_unknown",
            failure="refund_indeterminate_needs_human",
            resolution=str(Resolution.UNKNOWN),
        )

        body = client.get("/dead-letters").json()
        assert body["count"] == 2
        # Surfaced separately because these are the ones an operator cannot
        # act on from the console alone.
        assert body["needing_investigation"] == 1

    def test_a_listing_names_the_captured_state_without_shipping_it(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        file_letter(deps)
        row = client.get("/dead-letters").json()["dead_letters"][0]
        assert "state" not in row
        assert row["state_keys"] == ["proposal", "reconciliation", "triage"]

    def test_fetching_one_letter_includes_the_state_a_replay_needs(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        letter = file_letter(deps)
        body = client.get(f"/dead-letters/{letter.letter_id}").json()
        assert body["state"]["proposal"]["capture_id"] == "cap_9120B1"

    def test_an_unknown_letter_is_a_404(self, client: TestClient) -> None:
        assert client.get("/dead-letters/dlq_nope").status_code == 404


class TestHandling:
    def test_a_clean_failure_can_be_replayed(self, deps: Dependencies, client: TestClient) -> None:
        letter = file_letter(deps)
        response = client.post(
            f"/dead-letters/{letter.letter_id}/handle",
            json={"action": "replay", "handled_by": "dana@acme"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == str(LetterStatus.REPLAYED)
        assert body["replay_run_id"] == "run_000001_replay"

    def test_an_unknown_effect_cannot_be_replayed(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        # The assertion this endpoint exists for. On the other side of a
        # warning people click through is a second refund.
        letter = file_letter(
            deps,
            failure="refund_indeterminate_needs_human",
            resolution=str(Resolution.UNKNOWN),
        )
        response = client.post(
            f"/dead-letters/{letter.letter_id}/handle",
            json={"action": "replay", "handled_by": "dana@acme"},
        )
        assert response.status_code == 409
        assert "unknown" in response.json()["detail"]

        # And it is still open, so the refusal did not consume it.
        row = deps.dead_letters.get(letter.letter_id)
        assert row is not None and row.status is LetterStatus.OPEN

    def test_an_unreplayable_letter_can_still_be_closed(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        # Refusing the replay must not trap the letter: a person who checked
        # the gateway can resolve it.
        letter = file_letter(
            deps,
            failure="refund_indeterminate_needs_human",
            resolution=str(Resolution.UNKNOWN),
        )
        response = client.post(
            f"/dead-letters/{letter.letter_id}/handle",
            json={
                "action": "resolve",
                "handled_by": "dana@acme",
                "note": "gateway shows no refund; reissued by hand",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == str(LetterStatus.RESOLVED)

    def test_a_handled_letter_is_not_handled_twice(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        letter = file_letter(deps)
        first = client.post(
            f"/dead-letters/{letter.letter_id}/handle",
            json={"action": "abandon", "handled_by": "dana@acme"},
        )
        assert first.status_code == 200

        second = client.post(
            f"/dead-letters/{letter.letter_id}/handle",
            json={"action": "resolve", "handled_by": "sam@acme"},
        )
        assert second.status_code == 409
        assert "already" in second.json()["detail"]

    def test_a_decision_has_to_be_attributable(
        self, deps: Dependencies, client: TestClient
    ) -> None:
        letter = file_letter(deps)
        response = client.post(
            f"/dead-letters/{letter.letter_id}/handle",
            json={"action": "resolve", "handled_by": ""},
        )
        assert response.status_code == 422
