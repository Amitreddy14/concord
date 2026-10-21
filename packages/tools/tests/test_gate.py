"""Phase 2's three acceptance criteria, each asserted directly.

From the roadmap: a mutating tool called twice with the same arguments inside
one run produces one effect; scope violations raise before any network call;
and the circuit breaker opens and recovers under the hostile profile.

Everything here runs against the real mock services rather than stubs, so a
pass means the tool layer and the systems of record actually agree.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from apps.mocks.faults import FaultConfig, FaultProfile, ScriptedFault
from apps.mocks.serve import build
from evals.fixtures import Scenario
from packages.shared.ledger import InMemoryLedger, StepKind
from packages.tools.breaker import BreakerRegistry, BreakerState
from packages.tools.errors import ErrorKind, ScopeViolation, ToolError
from packages.tools.invoker import ToolInvoker
from packages.tools.tests.conftest import BASE_URLS, RoutingTransport, seed_all

# S03 has faults off, so these tests isolate idempotency from retry.
REFUND = {
    "capture_id": "cap_9120B1",
    "amount": {"amount_minor": 89000, "currency": "INR"},
    "reason_code": "buyer_request",
}

# S09 runs under the hostile profile with the accept-then-timeout script.
S09_REFUND = {
    "capture_id": "cap_5150F1",
    "amount": {"amount_minor": 199900, "currency": "INR"},
    "reason_code": "buyer_request",
}


class TestOneEffectFromARepeatedCall:
    """Criterion 1: same tool, same arguments, same run — one effect."""

    async def test_calling_twice_produces_one_refund(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        seed_all(services, scenarios["S03"])

        first = await invoker.call("create_refund", REFUND, run_id="run_a", node="action")
        second = await invoker.call("create_refund", REFUND, run_id="run_a", node="action")

        assert first.replayed is False
        assert second.replayed is True
        assert second.data["refund_id"] == first.data["refund_id"]

        store = services["payments"].state.store  # type: ignore[attr-defined]
        assert len(store.refunds) == 1

    async def test_argument_order_does_not_defeat_the_key(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        # The same request written differently is the same request. If key
        # order changed the key, any client library that reordered a dict
        # would silently cause a double refund.
        seed_all(services, scenarios["S03"])
        reordered = {
            "reason_code": "buyer_request",
            "amount": {"currency": "INR", "amount_minor": 89000},
            "capture_id": "cap_9120B1",
        }

        await invoker.call("create_refund", REFUND, run_id="run_b", node="action")
        second = await invoker.call("create_refund", reordered, run_id="run_b", node="action")

        assert second.replayed is True
        assert len(services["payments"].state.store.refunds) == 1  # type: ignore[attr-defined]

    async def test_a_different_run_is_a_different_effect(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        # The guarantee is per run, not global. Two merchants asking for the
        # same refund on different days must both be served, so the key must
        # not collapse across runs.
        seed_all(services, scenarios["S03"])
        partial = {**REFUND, "amount": {"amount_minor": 44500, "currency": "INR"}}

        await invoker.call("create_refund", partial, run_id="run_c", node="action")
        await invoker.call("create_refund", partial, run_id="run_d", node="action")

        assert len(services["payments"].state.store.refunds) == 2  # type: ignore[attr-defined]

    async def test_reads_carry_no_idempotency_key(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        seed_all(services, scenarios["S02"])
        result = await invoker.call(
            "get_capture", {"capture_id": "cap_8842A1"}, run_id="run_e", node="gather"
        )
        assert result.replayed is False
        assert result.data["capture_id"] == "cap_8842A1"


class TestScopeIsEnforcedBeforeTheNetwork:
    """Criterion 2: a scope violation never reaches a service."""

    @staticmethod
    def _restrict(invoker: ToolInvoker, without: str) -> None:
        from dataclasses import replace

        invoker.tenant = replace(
            invoker.tenant, tool_scopes=frozenset(invoker.tenant.tool_scopes - {without})
        )

    async def test_a_forbidden_scope_raises_scope_violation(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        seed_all(services, scenarios["S03"])
        self._restrict(invoker, "payments:refund:write")

        with pytest.raises(ScopeViolation) as caught:
            await invoker.call("create_refund", REFUND, run_id="run_f", node="action")

        assert caught.value.scope == "payments:refund:write"
        assert caught.value.tenant == "acme-payments"

    async def test_nothing_reaches_the_service(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        # The assertion that makes this criterion meaningful: not merely that
        # an exception was raised, but that the ledger shows no money moved
        # and the service was never asked to move it.
        seed_all(services, scenarios["S03"])
        self._restrict(invoker, "payments:refund:write")

        with pytest.raises(ScopeViolation):
            await invoker.call("create_refund", REFUND, run_id="run_g", node="action")

        assert len(services["payments"].state.store.refunds) == 0  # type: ignore[attr-defined]
        assert invoker.ledger.scopes_used("run_g") == set()

    async def test_the_refusal_is_recorded_with_what_was_attempted(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        seed_all(services, scenarios["S03"])
        self._restrict(invoker, "payments:refund:write")

        with pytest.raises(ScopeViolation):
            await invoker.call("create_refund", REFUND, run_id="run_h", node="action")

        steps = invoker.ledger.steps("run_h")
        assert steps, "a refused call is evidence, not an absence"
        assert any(s.error_code == "scope_violation" for s in steps)
        attempted = invoker.ledger.payload("run_h", 1, "input")
        assert attempted["would_require"] == "payments:refund:write"

    async def test_invalid_arguments_are_refused_before_the_network(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        seed_all(services, scenarios["S03"])

        with pytest.raises(ToolError) as caught:
            await invoker.call(
                "create_refund",
                {**REFUND, "capture_id": "pay_9120B"},  # a payment id, not a capture
                run_id="run_i",
                node="action",
            )

        assert caught.value.failure.kind is ErrorKind.POISONED_INPUT
        assert len(services["payments"].state.store.refunds) == 0  # type: ignore[attr-defined]

    async def test_an_unknown_argument_is_refused(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        # Relevant to S10: a model persuaded to add `skip_approval: true`
        # produces a validation failure, not a widened request.
        seed_all(services, scenarios["S03"])

        with pytest.raises(ToolError) as caught:
            await invoker.call(
                "create_refund",
                {**REFUND, "skip_approval": True},
                run_id="run_j",
                node="action",
            )

        assert caught.value.failure.kind is ErrorKind.POISONED_INPUT
        assert "skip_approval" in caught.value.failure.detail


class TestCircuitBreaker:
    """Criterion 3: the breaker opens under sustained failure and recovers."""

    @staticmethod
    async def _hostile_invoker(tenant, clock, sleeper, scenarios):  # type: ignore[no-untyped-def]
        """An invoker whose payments service always fails."""
        services = {
            name: build(name, FaultConfig(profile=FaultProfile.OFF))
            for name in ("crm", "payments", "ticketing", "knowledge")
        }
        seed_all(services, scenarios["S02"])

        # The script goes on *after* seeding: /_test/reset clears the
        # injector, so installing it first would have it wiped. Covering more
        # attempts than any test makes means the dependency is reliably down
        # rather than probabilistically so.
        services["payments"].state.injector.load_script(  # type: ignore[attr-defined]
            [ScriptedFault("GET /captures/{capture_id}", n, "server_error") for n in range(1, 40)]
        )
        apps = {
            httpx.URL(u).host: a
            for u, a in zip(BASE_URLS.values(), services.values(), strict=False)
        }
        client = httpx.AsyncClient(transport=RoutingTransport(apps))
        invoker = ToolInvoker(
            tenant=tenant,
            base_urls=dict(BASE_URLS),
            client=client,
            ledger=InMemoryLedger(),
            breakers=BreakerRegistry(
                failure_threshold=3, cooldown=timedelta(seconds=30), clock=clock
            ),
            sleep=sleeper,
        )
        return invoker, services, client

    async def test_the_breaker_opens_then_recovers(
        self, tenant, clock, sleeper, scenarios: dict[str, Scenario]
    ) -> None:
        invoker, services, client = await self._hostile_invoker(tenant, clock, sleeper, scenarios)
        try:
            breaker = invoker.breakers.for_dependency("payments")

            # Each call exhausts its retry budget, so the breaker reaches its
            # threshold quickly.
            for _ in range(3):
                with pytest.raises(ToolError):
                    await invoker.call(
                        "get_capture", {"capture_id": "cap_8842A1"}, run_id="run_k", node="gather"
                    )

            assert breaker.state is BreakerState.OPEN

            # Open means refused without a network call.
            before = len(invoker.ledger.steps("run_l"))
            with pytest.raises(ToolError) as caught:
                await invoker.call(
                    "get_capture", {"capture_id": "cap_8842A1"}, run_id="run_l", node="gather"
                )
            assert caught.value.failure.code == "circuit_open"
            assert len(invoker.ledger.steps("run_l")) > before

            # Another dependency is unaffected: degraded, not dead.
            merchant = await invoker.call(
                "get_merchant", {"merchant_id": "M-1007"}, run_id="run_m", node="gather"
            )
            assert merchant.data["merchant_id"] == "M-1007"
            assert invoker.breakers.for_dependency("crm").state is BreakerState.CLOSED

            # After the cooldown the breaker probes, and a healthy dependency
            # closes it.
            clock.advance(31)
            assert breaker.state is BreakerState.HALF_OPEN

            services["payments"].state.injector.load_script([])  # type: ignore[attr-defined]
            recovered = await invoker.call(
                "get_capture", {"capture_id": "cap_8842A1"}, run_id="run_n", node="gather"
            )
            assert recovered.data["capture_id"] == "cap_8842A1"
            assert breaker.state is BreakerState.CLOSED
        finally:
            await client.aclose()

    async def test_a_client_error_does_not_trip_the_breaker(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        # A 404 is the dependency working correctly and telling us something
        # true. Counting it toward the breaker would trip the circuit on our
        # own bad requests and hide the real bug behind an outage.
        seed_all(services, scenarios["S02"])

        for i in range(5):
            with pytest.raises(ToolError):
                await invoker.call(
                    "get_capture", {"capture_id": "cap_missing"}, run_id=f"run_o{i}", node="gather"
                )

        assert invoker.breakers.for_dependency("payments").state is BreakerState.CLOSED


class TestLedger:
    async def test_every_attempt_is_recorded_around_the_call(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        seed_all(services, scenarios["S02"])
        await invoker.call(
            "get_capture", {"capture_id": "cap_8842A1"}, run_id="run_p", node="gather"
        )

        steps = invoker.ledger.steps("run_p")
        assert [s.outcome for s in steps] == ["started", "success"]
        assert all(s.kind is StepKind.TOOL_CALL for s in steps)
        assert steps[0].scope == "payments:capture:read"
        assert steps[-1].output_digest is not None

    async def test_scopes_used_reflects_only_successful_calls(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        seed_all(services, scenarios["S02"])
        await invoker.call("get_merchant", {"merchant_id": "M-1007"}, run_id="run_q", node="gather")
        await invoker.call(
            "get_capture", {"capture_id": "cap_8842A1"}, run_id="run_q", node="gather"
        )
        assert invoker.ledger.scopes_used("run_q") == {
            "crm:merchant:read",
            "payments:capture:read",
        }
