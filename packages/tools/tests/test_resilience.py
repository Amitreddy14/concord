"""Retry, classification, and S09 end to end through the tool layer.

The gate tests cover phase 2's three stated criteria. These cover the
behaviour those criteria rest on, and the one scenario that exercises the
whole path at once.
"""

from __future__ import annotations

import pytest

from evals.fixtures import Scenario
from packages.tools.errors import ErrorKind, ToolError, classify
from packages.tools.invoker import ToolInvoker
from packages.tools.keys import derive_idempotency_key
from packages.tools.retry import RetryClass, RetryPolicy, delay_for_attempt, schedule
from packages.tools.tests.conftest import RecordingSleep, seed_all

S09_REFUND = {
    "capture_id": "cap_5150F1",
    "amount": {"amount_minor": 199900, "currency": "INR"},
    "reason_code": "buyer_request",
}


class TestScenario09ThroughTheToolLayer:
    async def test_the_timeout_is_survived_with_one_refund(
        self,
        invoker: ToolInvoker,
        services: dict[str, object],
        scenarios: dict[str, Scenario],
        sleeper: RecordingSleep,
    ) -> None:
        # The first attempt is accepted upstream and times out at the client;
        # the retry carries the same derived key and gets the original back.
        # The caller sees a success and never learns there was a problem,
        # which is the point.
        seed_all(services, scenarios["S09"])

        result = await invoker.call("create_refund", S09_REFUND, run_id="run_s09", node="action")

        assert result.attempts == 2
        assert result.replayed is True
        assert len(services["payments"].state.store.refunds) == 1  # type: ignore[attr-defined]
        assert len(sleeper.delays) == 1, "exactly one backoff between the two attempts"

    async def test_the_ledger_shows_both_attempts(
        self, invoker: ToolInvoker, services: dict[str, object], scenarios: dict[str, Scenario]
    ) -> None:
        # A reviewer asking what happened must see the failed attempt. A run
        # that quietly reports success hides the fact that a refund was
        # applied by an attempt the caller never got a response to.
        seed_all(services, scenarios["S09"])
        await invoker.call("create_refund", S09_REFUND, run_id="run_s09b", node="action")

        steps = invoker.ledger.steps("run_s09b")
        outcomes = [s.outcome for s in steps]
        assert "error" in outcomes, "the timed-out attempt must be visible"
        assert outcomes[-1] == "success"
        assert {s.attempt for s in steps} == {1, 2}

    async def test_the_timeout_is_classified_as_indeterminate(
        self,
        invoker: ToolInvoker,
        services: dict[str, object],
        scenarios: dict[str, Scenario],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Not merely "retryable". The service said the effect was applied, and
        # phase 7's reconciliation pass keys on that distinction.
        seed_all(services, scenarios["S09"])

        # Retry is disabled for this one call so the classified failure
        # surfaces instead of being absorbed by the successful second attempt.
        no_retry = RetryPolicy.for_class(RetryClass.NONE)
        monkeypatch.setattr(RetryPolicy, "for_class", classmethod(lambda cls, rc: no_retry))

        with pytest.raises(ToolError) as caught:
            await invoker.call("create_refund", S09_REFUND, run_id="run_s09c", node="action")

        assert caught.value.failure.kind is ErrorKind.INDETERMINATE
        assert caught.value.failure.effect_applied is True
        # And the effect really did happen, which is why it is indeterminate
        # rather than a plain failure.
        assert len(services["payments"].state.store.refunds) == 1  # type: ignore[attr-defined]


class TestRetrySchedule:
    def test_reads_get_more_attempts_than_mutations(self) -> None:
        assert (
            RetryPolicy.for_class(RetryClass.READ).max_attempts
            > RetryPolicy.for_class(RetryClass.MUTATION).max_attempts
        )

    def test_the_schedule_is_reproducible_within_a_run(self) -> None:
        # Replay in phase 7 depends on this: a replayed run whose backoff
        # differs is not a replay of the same run.
        policy = RetryPolicy.for_class(RetryClass.READ)
        first = schedule(policy, run_id="run_1", node="gather", tool="get_capture")
        second = schedule(policy, run_id="run_1", node="gather", tool="get_capture")
        assert first == second

    def test_different_runs_get_different_jitter(self) -> None:
        # The herd-spreading property jitter exists for, preserved despite
        # being deterministic per run.
        policy = RetryPolicy.for_class(RetryClass.READ)
        a = schedule(policy, run_id="run_1", node="gather", tool="get_capture")
        b = schedule(policy, run_id="run_2", node="gather", tool="get_capture")
        assert a != b

    def test_delays_grow_and_stay_under_the_cap(self) -> None:
        policy = RetryPolicy.for_class(RetryClass.READ)
        delays = schedule(policy, run_id="run_1", node="n", tool="t")
        assert delays == sorted(delays)
        assert all(d <= policy.max_delay_ms for d in delays)

    def test_total_delay_is_bounded(self) -> None:
        # A merchant waiting on a chat reply is not helped by a tool layer
        # that is still being patient.
        policy = RetryPolicy.for_class(RetryClass.MUTATION)
        assert sum(schedule(policy, run_id="r", node="n", tool="t")) <= policy.max_total_delay_ms

    def test_retry_after_overrides_the_computed_backoff(self) -> None:
        policy = RetryPolicy.for_class(RetryClass.READ)
        computed = delay_for_attempt(policy, 2, run_id="r", node="n", tool="t")
        honoured = delay_for_attempt(policy, 2, run_id="r", node="n", tool="t", retry_after_s=2)
        assert honoured == 2000
        assert honoured != computed


class TestClassification:
    @pytest.mark.parametrize(
        "status,code,expected",
        [
            (429, "rate_limited", ErrorKind.RETRYABLE),
            (500, "upstream_failure", ErrorKind.RETRYABLE),
            (504, "upstream_timeout", ErrorKind.INDETERMINATE),
            (404, "not_found", ErrorKind.FATAL),
            (409, "idempotency_key_reused", ErrorKind.FATAL),
            (422, "not_permissible", ErrorKind.NEEDS_HUMAN),
            (422, "citations_required", ErrorKind.NEEDS_HUMAN),
            (422, "illegal_transition", ErrorKind.FATAL),
        ],
    )
    def test_status_and_code_map_to_a_kind(
        self, status: int, code: str, expected: ErrorKind
    ) -> None:
        failure = classify(tool="t", status=status, problem={"code": code, "detail": "d"})
        assert failure.kind is expected

    def test_a_transport_failure_is_indeterminate_not_retryable(self) -> None:
        # Nothing came back, so whether the request was received is unknowable
        # from here. Calling that "retryable" would be an optimistic guess
        # about whether an effect happened.
        failure = classify(tool="t", status=None, problem=None, transport_error="reset")
        assert failure.kind is ErrorKind.INDETERMINATE

    def test_an_illegal_transition_is_fatal_not_needs_human(self) -> None:
        # Both are 422, but they mean different things: a refund outside
        # policy wants a person, while a wrong graph edge wants a code fix.
        # The code field is what separates them.
        failure = classify(tool="t", status=422, problem={"code": "illegal_transition"})
        assert failure.kind is ErrorKind.FATAL
        assert not failure.retryable


class TestKeyDerivation:
    def test_the_same_request_in_one_run_gives_one_key(self) -> None:
        args = {"capture_id": "cap_1", "amount": {"amount_minor": 100, "currency": "INR"}}
        assert derive_idempotency_key(
            run_id="r", node="n", tool="create_refund", arguments=args
        ) == derive_idempotency_key(run_id="r", node="n", tool="create_refund", arguments=args)

    def test_key_order_in_the_arguments_is_irrelevant(self) -> None:
        a = derive_idempotency_key(
            run_id="r",
            node="n",
            tool="t",
            arguments={"capture_id": "cap_1", "amount": {"amount_minor": 1, "currency": "INR"}},
        )
        b = derive_idempotency_key(
            run_id="r",
            node="n",
            tool="t",
            arguments={"amount": {"currency": "INR", "amount_minor": 1}, "capture_id": "cap_1"},
        )
        assert a == b

    @pytest.mark.parametrize(
        "field,value",
        [("run_id", "other"), ("node", "other"), ("tool", "other")],
    )
    def test_each_component_changes_the_key(self, field: str, value: str) -> None:
        base = {"run_id": "r", "node": "n", "tool": "t", "arguments": {"a": 1}}
        assert derive_idempotency_key(**base) != derive_idempotency_key(**{**base, field: value})

    def test_a_missing_component_is_refused(self) -> None:
        with pytest.raises(ValueError, match="required"):
            derive_idempotency_key(run_id="", node="n", tool="t", arguments={})
