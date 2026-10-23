"""The error taxonomy, checked for totality rather than sampled.

Phase 7's deliverable is "a mapping from every dependency failure mode", and
the word doing the work is *every*. A test that checks six interesting cases
proves the interesting cases work; it says nothing about the failure mode
somebody adds next quarter, which is precisely the one that will be
misclassified.

So these tests enumerate. Every `ProblemCode` the services can emit is pulled
out of `packages.shared.problem` by reflection, and each one has to classify
to something deliberate. A new problem code added without a thought about how
the tool layer should react fails here.

The consistency check is the one that earned its place immediately: it found
that `problem.py` declared `state_conflict` non-retryable while the classifier
mapped it to RETRYABLE. Two tables describing the same failure and
disagreeing, which is the same shape as the phase 6 bug where the risk tier
table and the policy engine both decided what approval an action needed.
"""

from __future__ import annotations

import inspect

import pytest

from packages.shared import problem as problem_module
from packages.shared.problem import ProblemCode
from packages.tools.errors import ErrorKind, ToolFailure, classify

ALL_PROBLEM_CODES: list[ProblemCode] = [
    value for _, value in inspect.getmembers(problem_module) if isinstance(value, ProblemCode)
]


def _classify(code: ProblemCode) -> ToolFailure:
    return classify(
        tool="create_refund",
        status=code.status,
        problem={"code": code.code, "detail": code.title},
    )


class TestEveryProblemCodeIsClassified:
    def test_the_table_is_not_empty(self) -> None:
        # Reflection that silently finds nothing would make every test below
        # pass vacuously, which is how the dependency checker once passed
        # while scanning no files at all.
        assert len(ALL_PROBLEM_CODES) >= 10

    @pytest.mark.parametrize("code", ALL_PROBLEM_CODES, ids=lambda c: c.code)
    def test_each_code_classifies(self, code: ProblemCode) -> None:
        failure = _classify(code)
        assert failure.kind in set(ErrorKind)
        assert failure.code == code.code

    @pytest.mark.parametrize("code", ALL_PROBLEM_CODES, ids=lambda c: c.code)
    def test_retryability_agrees_with_the_problem_table(self, code: ProblemCode) -> None:
        # `ProblemCode.retryable` is what the service tells a client; the
        # error kind is what the tool layer does about it. They describe the
        # same failure, so they must not disagree — and they did, for
        # `state_conflict`, until this test was written.
        failure = _classify(code)
        assert failure.retryable == code.retryable, (
            f"{code.code}: problem.py says retryable={code.retryable}, "
            f"but it classifies as {failure.kind} (retryable={failure.retryable})"
        )


class TestStatusClassDefaults:
    @pytest.mark.parametrize("status", [500, 502, 503, 507, 521, 599])
    def test_an_unlisted_5xx_is_retryable(self, status: int) -> None:
        # The old fallback was a flat FATAL, so a 507 or a 521 was never
        # retried — the one class of failure where another attempt is most
        # likely to succeed.
        assert _classify_status(status).kind is ErrorKind.RETRYABLE

    @pytest.mark.parametrize("status", [400, 404, 418, 451])
    def test_an_unlisted_4xx_is_fatal(self, status: int) -> None:
        # The request is the problem. Repeating it unchanged changes nothing.
        assert _classify_status(status).kind is ErrorKind.FATAL

    def test_a_request_timeout_is_indeterminate_not_retryable(self) -> None:
        # 408 means the server gave up waiting. It probably applied nothing,
        # and "probably" is not "did not" — for a mutation that difference is
        # a second refund.
        assert _classify_status(408).kind is ErrorKind.INDETERMINATE

    def test_a_gateway_timeout_is_indeterminate(self) -> None:
        # The whole of S09: the effect may have landed upstream.
        assert _classify_status(504).kind is ErrorKind.INDETERMINATE


class TestTransportFailures:
    def test_no_response_is_indeterminate_rather_than_retryable(self) -> None:
        # Nothing came back, so whether the request was received is
        # unknowable from here. Optimism costs a duplicate refund.
        failure = classify(
            tool="create_refund",
            status=None,
            problem=None,
            transport_error="connection reset by peer",
        )
        assert failure.kind is ErrorKind.INDETERMINATE
        assert failure.retryable

    def test_an_indeterminate_failure_does_not_claim_the_effect_was_applied(self) -> None:
        # Absent means unknown, and unknown is not the same as no. A
        # reconciliation pass has to be able to tell those apart.
        failure = classify(tool="create_refund", status=504, problem={"code": "upstream_timeout"})
        assert failure.effect_applied is None

    def test_the_service_can_state_the_effect_was_applied(self) -> None:
        failure = classify(
            tool="create_refund",
            status=504,
            problem={"code": "upstream_timeout", "effect_applied": True},
        )
        assert failure.effect_applied is True


class TestKindSemantics:
    def test_only_retryable_and_indeterminate_are_retryable(self) -> None:
        # The distinction the taxonomy exists for. If a third kind ever
        # becomes retryable, it is a decision someone should have to make
        # here rather than discover in production.
        retryable = {k for k in ErrorKind if _failure(k).retryable}
        assert retryable == {ErrorKind.RETRYABLE, ErrorKind.INDETERMINATE}

    def test_poisoned_input_never_reached_the_network(self) -> None:
        # Which is why it is separate from FATAL: there is nothing upstream
        # to reconcile and nothing to compensate.
        assert not _failure(ErrorKind.POISONED_INPUT).retryable


def _classify_status(status: int) -> ToolFailure:
    return classify(tool="create_refund", status=status, problem={"code": f"http_{status}"})


def _failure(kind: ErrorKind) -> ToolFailure:
    return ToolFailure(kind=kind, code="x", status=None, detail="", tool="t")
