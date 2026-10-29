"""The two checks the demo walkthrough produced, tested directly.

`approval_outcome_reached_the_merchant` is exercised by S04's rejected and
expired branches, and removing the fix makes both fail — that one is covered
by the scenario suite.

`reviewer_note_never_reaches_the_merchant` is not, and cannot be. Reaching it
through a scenario needs a drafter that quotes an internal note, and the
deterministic stand-in will not do that: the note is never in the facts
block, which is the architecture working. So the check would sit permanently
green having never seen a leak, which is indistinguishable from a check that
cannot detect one.

These tests construct the leak instead. A check that has never failed is not
a check.
"""

from __future__ import annotations

from typing import Any

from evals.fixtures import Assertion, load_all
from evals.harness.checks import CHECKS
from evals.harness.outcome import Outcome
from packages.agent.deadletter import InMemoryDeadLetterQueue
from packages.policy.approval import ApprovalStatus, InMemoryApprovalStore, build_request
from packages.shared.ledger import InMemoryLedger

NOTE = "internal: merchant has three prior chargebacks on this MID, declining"
TICKET = "tkt_1"


def _store_with_rejection() -> InMemoryApprovalStore:
    store = InMemoryApprovalStore()
    request = store.upsert(
        build_request(
            run_id="run_1",
            ticket_id=TICKET,
            thread_id=f"ticket:{TICKET}",
            tenant_id="acme-payments",
            merchant_id="M-1012",
            decision={"permission": "needs_approval", "rule_id": "R2-elevated-refund"},
            proposal={"kind": "create_refund"},
            evidence={},
            expires_after_hours=48,
        )
    )
    store.decide(request.approval_id, status=ApprovalStatus.REJECTED, reviewer="a-human", note=NOTE)
    return store


def _outcome(messages: dict[str, dict[str, Any]]) -> Outcome:
    return Outcome(
        scenario=next(iter(load_all().values())),
        run_id="run_1",
        ticket_id=TICKET,
        state={"approval": {"status": "rejected", "approval_id": "apr_1"}},
        ledger=InMemoryLedger(),
        approvals=_store_with_rejection(),
        dead_letters=InMemoryDeadLetterQueue(),
        messages=messages,
    )


def _outbound(body: str) -> dict[str, dict[str, Any]]:
    return {"msg_1": {"ticket_id": TICKET, "direction": "outbound", "body": body}}


class TestTheNoteStaysInternal:
    def test_a_leaked_note_is_caught(self) -> None:
        outcome = _outcome(_outbound(f"We could not refund this. {NOTE}"))
        result = CHECKS["reviewer_note_never_reaches_the_merchant"](
            outcome, Assertion(name="reviewer_note_never_reaches_the_merchant", value=None)
        )
        assert not result.ok
        assert "internal note" in result.detail

    def test_an_outcome_without_the_note_passes(self) -> None:
        # The reply says what happened and not why, which is the shape the
        # other check requires and this one permits.
        outcome = _outcome(_outbound("A person reviewed this and no refund has been issued."))
        result = CHECKS["reviewer_note_never_reaches_the_merchant"](
            outcome, Assertion(name="reviewer_note_never_reaches_the_merchant", value=None)
        )
        assert result.ok

    def test_the_note_on_an_internal_message_is_not_a_leak(self) -> None:
        """The note belongs on the ticket. `reviewer_note_attached_to_ticket`
        requires it there, and this check must not contradict that one."""
        messages = {
            "msg_1": {"ticket_id": TICKET, "direction": "internal", "body": NOTE},
            "msg_2": {
                "ticket_id": TICKET,
                "direction": "outbound",
                "body": "No refund has been issued.",
            },
        }
        result = CHECKS["reviewer_note_never_reaches_the_merchant"](
            _outcome(messages), Assertion(name="x", value=None)
        )
        assert result.ok

    def test_a_note_on_another_ticket_is_not_this_merchants_leak(self) -> None:
        messages = {
            "msg_1": {"ticket_id": "tkt_other", "direction": "outbound", "body": NOTE},
        }
        result = CHECKS["reviewer_note_never_reaches_the_merchant"](
            _outcome(messages), Assertion(name="x", value=None)
        )
        assert result.ok


class TestTheOutcomeMustBeStated:
    def test_silence_is_a_failure(self) -> None:
        result = CHECKS["approval_outcome_reached_the_merchant"](
            _outcome(_outbound("Thanks for getting in touch. Let me know if anything is unclear.")),
            Assertion(name="x", value=None),
        )
        assert not result.ok
        assert "says nothing about it" in result.detail

    def test_nothing_sent_at_all_is_a_failure(self) -> None:
        result = CHECKS["approval_outcome_reached_the_merchant"](
            _outcome({}), Assertion(name="x", value=None)
        )
        assert not result.ok
        assert "nothing was sent" in result.detail

    def test_a_run_with_no_unfulfilled_approval_is_not_judged(self) -> None:
        outcome = Outcome(
            scenario=next(iter(load_all().values())),
            run_id="run_1",
            ticket_id=TICKET,
            state={"approval": {"status": "approved", "approval_id": "apr_1"}},
            ledger=InMemoryLedger(),
            approvals=InMemoryApprovalStore(),
            dead_letters=InMemoryDeadLetterQueue(),
        )
        result = CHECKS["approval_outcome_reached_the_merchant"](
            outcome, Assertion(name="x", value=None)
        )
        assert result.ok

    def test_the_claim_is_matched_not_a_form_of_words(self) -> None:
        """Three different wordings of the same fact must all pass.

        A check that demanded one phrasing would be asserting the stand-in's
        prose, and would fail the moment a real model wrote the same thing
        differently.
        """
        for wording in (
            "No refund has been issued on this request.",
            "This was declined after review.",
            "The refund has not been issued.",
        ):
            result = CHECKS["approval_outcome_reached_the_merchant"](
                _outcome(_outbound(wording)), Assertion(name="x", value=None)
            )
            assert result.ok, f"{wording!r} states the outcome and was marked a failure"


def test_an_expired_approval_is_held_to_the_same_standard() -> None:
    outcome = Outcome(
        scenario=next(iter(load_all().values())),
        run_id="run_1",
        ticket_id=TICKET,
        state={"approval": {"status": "expired", "approval_id": "apr_1"}},
        ledger=InMemoryLedger(),
        approvals=InMemoryApprovalStore(),
        dead_letters=InMemoryDeadLetterQueue(),
        messages=_outbound("Thanks for getting in touch."),
    )
    result = CHECKS["approval_outcome_reached_the_merchant"](
        outcome, Assertion(name="x", value=None)
    )
    assert not result.ok
    assert "expired" in result.detail
