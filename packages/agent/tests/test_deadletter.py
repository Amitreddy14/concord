"""The dead-letter queue, and the one letter it refuses to replay.

The interesting behaviour is the refusal. A run that failed with no effect
upstream can be replayed freely; a run whose effect is unknown must not be,
because the replay re-attempts a mutation that may already have landed. The
queue refuses that outright rather than warning about it, on the grounds that
a warning on a replay button is a warning people click through.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from packages.agent.deadletter import (
    DeadLetterError,
    InMemoryDeadLetterQueue,
    LetterStatus,
    from_failed_run,
    letter_id_for,
)
from packages.tools.reconcile import Resolution

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


def letter(
    *,
    run_id: str = "run_1",
    failure: str = "refund_failed_without_effect",
    reconciliation: dict[str, object] | None = None,
    offset_minutes: int = 0,
):
    state: dict[str, object] = {
        "triage": {"intent": "refund_request"},
        "proposal": {"capture_id": "cap_1"},
    }
    if reconciliation is not None:
        state["reconciliation"] = reconciliation
    return from_failed_run(
        run_id=run_id,
        ticket_id="tkt_1",
        tenant_id="acme-payments",
        merchant_id="M-1012",
        failure=failure,
        state=state,
        now=NOW + timedelta(minutes=offset_minutes),
    )


class TestIdentity:
    def test_a_letter_is_one_per_run(self) -> None:
        # A failure reported twice must not become two pieces of work for an
        # operator.
        queue = InMemoryDeadLetterQueue()
        first = queue.add(letter())
        second = queue.add(letter())
        assert first.letter_id == second.letter_id == letter_id_for("run_1")
        assert len(list(queue.all())) == 1

    def test_a_replayed_run_gets_its_own_letter(self) -> None:
        queue = InMemoryDeadLetterQueue()
        queue.add(letter(run_id="run_1"))
        queue.add(letter(run_id="run_1_replay"))
        assert len(list(queue.all())) == 2


class TestWhatIsSafeToReplay:
    def test_a_failure_with_no_effect_is_replayable(self) -> None:
        # Read successfully, nothing upstream. Replaying cannot duplicate
        # what never happened.
        row = letter(
            failure="refund_failed_without_effect",
            reconciliation={"resolution": str(Resolution.ABSENT)},
        )
        assert not row.effect_is_unknown
        assert row.replayable

    def test_an_unknown_effect_is_not_replayable(self) -> None:
        # The assertion this module exists for.
        row = letter(
            failure="refund_indeterminate_needs_human",
            reconciliation={"resolution": str(Resolution.UNKNOWN)},
        )
        assert row.effect_is_unknown
        assert not row.replayable

    def test_the_failure_alone_is_enough_to_refuse(self) -> None:
        # Even with no reconciliation recorded — a run that died before it
        # could read — the failure name marks it unsafe.
        row = letter(failure="refund_indeterminate_needs_human", reconciliation=None)
        assert not row.replayable

    def test_the_queue_refuses_rather_than_warns(self) -> None:
        queue = InMemoryDeadLetterQueue()
        row = queue.add(
            letter(
                failure="refund_indeterminate_needs_human",
                reconciliation={"resolution": str(Resolution.UNKNOWN)},
            )
        )
        with pytest.raises(DeadLetterError, match="not safe to replay"):
            queue.mark(
                row.letter_id,
                status=LetterStatus.REPLAYED,
                handled_by="dana@acme",
                replay_run_id="run_2",
            )

    def test_an_unsafe_letter_can_still_be_closed_by_a_human(self) -> None:
        # Refusing the replay must not trap the letter. A person who checked
        # the gateway can resolve it.
        queue = InMemoryDeadLetterQueue()
        row = queue.add(letter(failure="refund_indeterminate_needs_human"))
        closed = queue.mark(
            row.letter_id,
            status=LetterStatus.RESOLVED,
            handled_by="dana@acme",
            note="gateway shows no refund; reissued by hand",
            now=NOW,
        )
        assert closed.status is LetterStatus.RESOLVED
        assert closed.handled_by == "dana@acme"


class TestHandling:
    def test_a_handled_letter_is_not_handled_twice(self) -> None:
        queue = InMemoryDeadLetterQueue()
        row = queue.add(letter())
        queue.mark(
            row.letter_id,
            status=LetterStatus.REPLAYED,
            handled_by="dana@acme",
            replay_run_id="run_1_replay",
        )
        with pytest.raises(DeadLetterError, match="already"):
            queue.mark(row.letter_id, status=LetterStatus.ABANDONED, handled_by="sam@acme")

    def test_open_letters_are_oldest_first(self) -> None:
        # An operator working the list top-down should not have it reorder
        # under them as failures arrive.
        queue = InMemoryDeadLetterQueue()
        queue.add(letter(run_id="run_b", offset_minutes=10))
        queue.add(letter(run_id="run_a", offset_minutes=0))
        assert [r.run_id for r in queue.open_letters()] == ["run_a", "run_b"]

    def test_a_handled_letter_leaves_the_open_list(self) -> None:
        queue = InMemoryDeadLetterQueue()
        row = queue.add(letter())
        queue.mark(row.letter_id, status=LetterStatus.RESOLVED, handled_by="dana@acme")
        assert queue.open_letters() == []

    def test_marking_an_unknown_letter_raises(self) -> None:
        queue = InMemoryDeadLetterQueue()
        with pytest.raises(DeadLetterError, match="no dead letter"):
            queue.mark("dlq_nope", status=LetterStatus.RESOLVED, handled_by="dana@acme")


class TestThePayload:
    def test_the_listing_names_the_captured_state_without_carrying_it(self) -> None:
        # The state is for the replay, not for a queue listing, and shipping
        # it in every row would make the list expensive to read.
        payload = letter().to_payload()
        assert "state" not in payload
        assert payload["state_keys"] == ["proposal", "triage"]
        assert payload["replayable"] is True

    def test_the_payload_says_whether_a_replay_is_safe(self) -> None:
        payload = letter(failure="refund_indeterminate_needs_human").to_payload()
        assert payload["replayable"] is False
        assert payload["effect_is_unknown"] is True
