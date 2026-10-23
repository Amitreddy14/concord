"""Reconciliation: finding out whether an indeterminate effect landed.

The test that matters here is the one where the read fails. Everything else
is arithmetic on a payload; `UNKNOWN` is where the money is, because the step
after `ABSENT` is to try again and trying again on an effect that did land is
the duplicate refund the whole phase exists to prevent.
"""

from __future__ import annotations

import pytest

from evals.fixtures import Scenario
from packages.tools.errors import ErrorKind, ToolFailure
from packages.tools.invoker import ToolInvoker
from packages.tools.keys import derive_idempotency_key
from packages.tools.reconcile import (
    COMPENSATION_FOR_TOOL,
    Compensation,
    Reconciliation,
    Resolution,
    needs_reconciliation,
    reconcile_refund,
)
from packages.tools.registry import TOOLS
from packages.tools.tests.conftest import seed_all

REFUND_ARGS = {
    "capture_id": "cap_9120B1",
    "amount": {"amount_minor": 89000, "currency": "INR"},
    "reason_code": "buyer_request",
    "initiated_by": "agent",
}


class TestTheCompensationTableIsTotal:
    def test_every_mutating_tool_has_a_strategy(self) -> None:
        # A tool added without a compensation decision would otherwise get
        # one by accident. "Nobody decided" and "no inverse exists" are
        # different facts and only one of them is acceptable.
        mutating = {name for name, spec in TOOLS.items() if spec.mutating}
        assert mutating, "the registry reported no mutating tools; the check would be vacuous"
        missing = mutating - set(COMPENSATION_FOR_TOOL)
        assert not missing, f"mutating tools with no compensation strategy: {sorted(missing)}"

    def test_no_strategy_is_recorded_for_a_tool_that_does_not_mutate(self) -> None:
        # The reverse drift: a strategy left behind after a tool stopped
        # mutating, implying a compensation nothing will ever call.
        mutating = {name for name, spec in TOOLS.items() if spec.mutating}
        stale = set(COMPENSATION_FOR_TOOL) - mutating
        assert not stale, f"compensation recorded for non-mutating tools: {sorted(stale)}"

    def test_money_has_no_inverse_and_says_so(self) -> None:
        # The contract has no cancel_refund. Claiming a compensation here
        # would be inventing an API, and a `compensate()` that quietly fails
        # is worse than one that refuses.
        assert COMPENSATION_FOR_TOOL["create_refund"] is Compensation.NONE

    def test_an_unknown_tool_raises_rather_than_defaulting(self) -> None:
        reconciliation = Reconciliation(
            resolution=Resolution.LANDED, tool="teleport_funds", idempotency_key="k"
        )
        with pytest.raises(ValueError, match="no compensation strategy"):
            _ = reconciliation.compensation


class TestWhenToReconcile:
    def test_an_indeterminate_mutation_is_worth_a_read(self) -> None:
        assert needs_reconciliation(_failure(ErrorKind.INDETERMINATE), mutating=True)

    def test_a_read_is_never_reconciled(self) -> None:
        # Nothing upstream changed, so there is nothing to find.
        assert not needs_reconciliation(_failure(ErrorKind.INDETERMINATE), mutating=False)

    @pytest.mark.parametrize(
        "kind",
        [ErrorKind.FATAL, ErrorKind.RETRYABLE, ErrorKind.NEEDS_HUMAN, ErrorKind.POISONED_INPUT],
    )
    def test_other_kinds_leave_nothing_to_find(self, kind: ErrorKind) -> None:
        # A FATAL is the dependency telling us something true, and a
        # POISONED_INPUT never reached the network.
        assert not needs_reconciliation(_failure(kind), mutating=True)

    def test_the_service_saying_it_did_not_apply_is_believed(self) -> None:
        # It knows and we do not. A read would cost a round trip to confirm
        # what the service already stated.
        failure = ToolFailure(
            kind=ErrorKind.INDETERMINATE,
            code="upstream_timeout",
            status=504,
            detail="",
            tool="create_refund",
            effect_applied=False,
        )
        assert not needs_reconciliation(failure, mutating=True)


class TestReadingTheSystemOfRecord:
    async def test_a_refund_that_landed_is_found_by_its_key(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        seed_all(services, scenarios["S03"])

        # Issue the refund exactly as a run would, so the key the service
        # stores is the key reconciliation will derive.
        result = await invoker.call("create_refund", REFUND_ARGS, run_id="run_r1", node="act")
        assert result.data["refund_id"]

        found = await reconcile_refund(
            invoker,
            run_id="run_r1",
            node="act",
            arguments=REFUND_ARGS,
            capture_id="cap_9120B1",
        )

        assert found.resolution is Resolution.LANDED
        assert found.effect is not None
        assert found.effect["refund_id"] == result.data["refund_id"]
        assert found.checked == ["get_capture", "get_payment"]

    async def test_a_refund_that_never_happened_is_absent(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        seed_all(services, scenarios["S03"])

        found = await reconcile_refund(
            invoker,
            run_id="run_r2",
            node="act",
            arguments=REFUND_ARGS,
            capture_id="cap_9120B1",
        )
        assert found.resolution is Resolution.ABSENT
        assert found.effect is None

    async def test_another_runs_refund_is_not_adopted(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        # The reason the match is on the key rather than on (capture, amount):
        # a different run refunding the same figure against the same capture
        # would otherwise be claimed as this run's effect, which is a worse
        # error than the one reconciliation is fixing.
        seed_all(services, scenarios["S03"])
        await invoker.call("create_refund", REFUND_ARGS, run_id="run_other", node="act")

        found = await reconcile_refund(
            invoker,
            run_id="run_mine",
            node="act",
            arguments=REFUND_ARGS,
            capture_id="cap_9120B1",
        )
        assert found.resolution is Resolution.ABSENT

        mine = derive_idempotency_key(
            run_id="run_mine", node="act", tool="create_refund", arguments=REFUND_ARGS
        )
        theirs = derive_idempotency_key(
            run_id="run_other", node="act", tool="create_refund", arguments=REFUND_ARGS
        )
        assert mine != theirs

    async def test_a_failed_read_is_unknown_and_never_absent(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        # The assertion this module exists for. ABSENT invites a retry;
        # UNKNOWN does not, and a reconciliation that cannot read must not
        # hand back the answer that licenses another attempt.
        seed_all(services, scenarios["S03"])

        found = await reconcile_refund(
            invoker,
            run_id="run_r3",
            node="act",
            arguments=REFUND_ARGS,
            capture_id="cap_does_not_exist",
        )

        assert found.resolution is Resolution.UNKNOWN
        assert found.resolution is not Resolution.ABSENT
        assert not found.is_settled
        assert found.read_failure is not None
        assert "could not read" in found.detail


def _failure(kind: ErrorKind) -> ToolFailure:
    return ToolFailure(kind=kind, code="x", status=None, detail="", tool="create_refund")
