"""Fault injection: determinism, and the S09 sequence.

Phase 1's definition of done is that S09's fault sequence reproduces from its
seed and yields exactly one refund every time. This module is that proof.

Why it matters more than it looks: a naive retry after a timeout creates a
second refund, and a second refund is a real financial loss that no amount of
later agent cleverness recovers. The property has to hold in the plumbing, not
in the prompt.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.mocks.faults import FaultConfig, FaultInjector, FaultProfile, ScriptedFault
from apps.mocks.serve import build
from apps.mocks.tests.conftest import seed
from evals.fixtures import Scenario, reset_payload

REFUND_BODY: dict[str, Any] = {
    "capture_id": "cap_5150F1",
    "amount": {"amount_minor": 199900, "currency": "INR"},
    "reason_code": "buyer_request",
    "initiated_by": "agent",
}
# Derived from the run and the call, exactly as the domain model specifies, so
# a replayed step produces the same key.
DERIVED_KEY = "run_s09-node_action-create_refund-8f21ca40"


class TestInjectorDeterminism:
    def test_the_same_seed_produces_the_same_sequence(self) -> None:
        sequences = []
        for _ in range(3):
            injector = FaultInjector(FaultProfile.HOSTILE, seed=1337)
            sequences.append(
                [injector.decide("POST", "/refunds").behaviour for _ in range(20)]
            )
        assert sequences[0] == sequences[1] == sequences[2]

    def test_a_different_seed_produces_a_different_sequence(self) -> None:
        a = FaultInjector(FaultProfile.HOSTILE, seed=1337)
        b = FaultInjector(FaultProfile.HOSTILE, seed=424242)
        assert [a.decide("POST", "/refunds").behaviour for _ in range(20)] != [
            b.decide("POST", "/refunds").behaviour for _ in range(20)
        ]

    def test_traffic_on_one_endpoint_does_not_perturb_another(self) -> None:
        # The reason the decision is a hash of (seed, endpoint, attempt)
        # rather than a draw from a shared RNG. With a shared RNG, each
        # endpoint's sequence would depend on the others' timing, and the
        # scenario would stop being reproducible the moment the agent made an
        # extra lookup.
        clean = FaultInjector(FaultProfile.HOSTILE, seed=1337)
        expected = [clean.decide("POST", "/refunds").behaviour for _ in range(10)]

        noisy = FaultInjector(FaultProfile.HOSTILE, seed=1337)
        observed = []
        for _ in range(10):
            noisy.decide("GET", "/payments")
            noisy.decide("GET", "/captures/{capture_id}")
            observed.append(noisy.decide("POST", "/refunds").behaviour)

        assert observed == expected

    def test_the_off_profile_injects_nothing(self) -> None:
        injector = FaultInjector(FaultProfile.OFF, seed=1337)
        assert all(injector.decide("POST", "/refunds").is_ok for _ in range(100))

    def test_a_script_overrides_the_profile(self) -> None:
        injector = FaultInjector(
            FaultProfile.HOSTILE,
            seed=1337,
            script=[ScriptedFault("POST /refunds", 1, "accept_then_client_timeout", 8000)],
        )
        first = injector.decide("POST", "/refunds")
        assert first.behaviour == "accept_then_client_timeout"
        assert first.scripted is True

    def test_reset_rewinds_the_attempt_counters(self) -> None:
        injector = FaultInjector(
            FaultProfile.OFF, seed=1337, script=[ScriptedFault("POST /refunds", 1, "server_error")]
        )
        assert injector.decide("POST", "/refunds").behaviour == "server_error"
        assert injector.decide("POST", "/refunds").behaviour == "ok"
        injector.reset()
        assert injector.decide("POST", "/refunds").behaviour == "server_error"


class TestScenario09:
    """The accept-then-timeout sequence, end to end through the service."""

    @staticmethod
    def client(scenario: Scenario) -> TestClient:
        app = build("payments", FaultConfig(profile=scenario.fault_profile, seed=scenario.fault_seed))
        client = TestClient(app)
        client.post("/_test/reset", json=reset_payload(scenario))
        return client

    def test_the_first_attempt_times_out_after_the_refund_is_applied(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        client = self.client(scenarios["S09"])
        first = client.post("/refunds", json=REFUND_BODY, headers={"Idempotency-Key": DERIVED_KEY})

        assert first.status_code == 504
        assert first.json()["code"] == "upstream_timeout"
        # The caller sees a failure, but the effect happened. That asymmetry
        # is the entire scenario.
        assert first.json()["effect_applied"] is True
        assert len(client.app.state.store.refunds) == 1

    def test_the_retry_returns_the_original_refund(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        client = self.client(scenarios["S09"])
        client.post("/refunds", json=REFUND_BODY, headers={"Idempotency-Key": DERIVED_KEY})
        retry = client.post("/refunds", json=REFUND_BODY, headers={"Idempotency-Key": DERIVED_KEY})

        assert retry.status_code == 201
        assert retry.headers.get("Idempotency-Replayed") == "true"
        assert len(client.app.state.store.refunds) == 1

    def test_exactly_one_refund_across_repeated_seeded_runs(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        # The fixture asks for five runs with an identical outcome. One run
        # producing one refund proves nothing about whether the key is derived
        # or merely happened not to collide.
        scenario = scenarios["S09"]
        assert scenario.repeat_runs == 5
        assert scenario.requires_identical_outcome

        outcomes = []
        for _ in range(scenario.repeat_runs):
            client = self.client(scenario)
            client.post("/refunds", json=REFUND_BODY, headers={"Idempotency-Key": DERIVED_KEY})
            client.post("/refunds", json=REFUND_BODY, headers={"Idempotency-Key": DERIVED_KEY})

            # Ground truth is read from the store, not through the API.
            # Under the hostile profile a GET can itself be faulted, and an
            # assertion that reads through an injected 500 is measuring the
            # fault rather than the ledger.
            store = client.app.state.store
            refunds = list(store.refunds.values())
            outcomes.append(
                (
                    len(refunds),
                    refunds[0]["amount"]["amount_minor"],
                    refunds[0]["capture_id"],
                    store.payments["pay_5150F"]["status"],
                )
            )

        assert all(o == (1, 199900, "cap_5150F1", "refunded") for o in outcomes), outcomes
        assert len(set(outcomes)) == 1, "every run must reach an identical outcome"

    def test_a_full_refund_is_also_caught_by_the_remainder_check(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        # Defence in depth, and worth knowing the shape of. When the first
        # refund consumes the whole capture, a retry with a *fresh* key is
        # refused anyway, because the unrefunded remainder is zero. The
        # idempotency key is not what saves this case.
        client = self.client(scenarios["S09"])
        client.post("/refunds", json=REFUND_BODY, headers={"Idempotency-Key": "attempt-1-random-aaa"})
        second = client.post(
            "/refunds", json=REFUND_BODY, headers={"Idempotency-Key": "attempt-2-random-bbb"}
        )
        assert second.status_code == 422
        assert second.json()["code"] == "not_permissible"
        assert len(client.app.state.store.refunds) == 1

    def test_a_partial_refund_with_fresh_keys_does_double(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        # The counter-test that matters, and the reason the remainder check is
        # not sufficient on its own. A partial refund leaves a remainder, so a
        # retry carrying a fresh key is perfectly permissible and the merchant
        # is refunded twice.
        #
        # This is the defect the derived-key rule exists to prevent. If this
        # test ever stops producing two refunds, the assertions above have
        # become vacuous and are passing for the wrong reason.
        client = self.client(scenarios["S09"])
        partial = {**REFUND_BODY, "amount": {"amount_minor": 99950, "currency": "INR"}}

        client.post("/refunds", json=partial, headers={"Idempotency-Key": "attempt-1-random-aaa"})
        client.post("/refunds", json=partial, headers={"Idempotency-Key": "attempt-2-random-bbb"})

        store = client.app.state.store
        assert len(store.refunds) == 2
        total = sum(r["amount"]["amount_minor"] for r in store.refunds.values())
        assert total == 199900, "the merchant has been refunded the full capture by two halves"

    def test_the_same_partial_refund_with_a_derived_key_does_not(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        # The same sequence with the key the domain model specifies.
        client = self.client(scenarios["S09"])
        partial = {**REFUND_BODY, "amount": {"amount_minor": 99950, "currency": "INR"}}

        client.post("/refunds", json=partial, headers={"Idempotency-Key": DERIVED_KEY})
        retry = client.post("/refunds", json=partial, headers={"Idempotency-Key": DERIVED_KEY})

        assert retry.headers.get("Idempotency-Replayed") == "true"
        assert len(client.app.state.store.refunds) == 1


class TestHostileProfileIsSurvivable:
    @pytest.mark.parametrize("profile", [FaultProfile.FLAKY, FaultProfile.HOSTILE])
    def test_a_retrying_caller_eventually_succeeds(
        self, profile: FaultProfile, scenarios: dict[str, Scenario]
    ) -> None:
        # Everything the profiles inject unscripted is retryable, so a caller
        # with bounded retries gets through. A profile that could not be
        # survived would make every scenario fail for the wrong reason.
        app = build("payments", FaultConfig(profile=profile, seed=1337))
        client = TestClient(app)
        client.post("/_test/reset", json=reset_payload(scenarios["S09"]))

        key = {"Idempotency-Key": "run_x-node_y-create_refund-deadbeef"}
        last = None
        for _ in range(8):
            last = client.post("/refunds", json=REFUND_BODY, headers=key)
            if last.status_code in (200, 201):
                break
        assert last is not None
        assert last.status_code == 201
        assert len(client.app.state.store.refunds) == 1
