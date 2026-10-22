"""Entity resolution, independent of any model.

Phase 5's definition of done requires the resolution order to be covered by
tests that do not involve the model. That is the point of implementing it as
pure functions: "which of these two payments did they mean" is a judgement
about evidence, and a judgement about evidence is checkable — unlike one
produced by sampling.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from packages.agent.resolution import (
    DECISIVE_MARGIN,
    Evidence,
    Outcome,
    clarifying_question,
    parse_amount,
    parse_last_four,
    resolve_payment,
)
from packages.shared.money import Money

NOW = datetime(2026, 10, 21, tzinfo=UTC)

# The S11 shape: one order_ref, two payments, eleven months apart.
OLDER: dict[str, Any] = {
    "payment_id": "pay_8842H",
    "merchant_id": "M-1052",
    "order_ref": "8842",
    "amount": {"amount_minor": 120000, "currency": "INR"},
    "method": "card",
    "buyer_reference": "5512",
    "created_at": "2025-11-20T10:00:00Z",
}
NEWER: dict[str, Any] = {
    "payment_id": "pay_8842J",
    "merchant_id": "M-1052",
    "order_ref": "8842",
    "amount": {"amount_minor": 340000, "currency": "INR"},
    "method": "upi",
    "created_at": "2026-10-10T16:30:00Z",
}


def resolve(
    evidence: Evidence, payments: list[dict[str, Any]] | None = None, merchant: str = "M-1052"
):  # type: ignore[no-untyped-def]
    return resolve_payment(
        payments=payments if payments is not None else [OLDER, NEWER],
        evidence=evidence,
        merchant_id=merchant,
        now=NOW,
    )


class TestResolutionOrder:
    def test_a_single_match_resolves(self) -> None:
        result = resolve(Evidence(order_ref="8842"), payments=[NEWER])
        assert result.outcome is Outcome.RESOLVED
        assert result.payment is not None
        assert result.payment["payment_id"] == "pay_8842J"

    def test_an_unknown_order_reference_is_not_found(self) -> None:
        # Not a near-match. Offering the closest payment to a reference that
        # does not exist invents a connection the merchant did not make.
        result = resolve(Evidence(order_ref="9999"))
        assert result.outcome is Outcome.NOT_FOUND
        assert result.payment is None

    def test_resolution_never_crosses_a_merchant_boundary(self) -> None:
        # Whatever the ticket claims. The merchant is authenticated; the
        # ticket body is not evidence about whose payment this is.
        assert resolve(Evidence(order_ref="8842"), merchant="M-9999").outcome is Outcome.NOT_FOUND

    def test_two_matches_with_nothing_to_separate_them_is_ambiguous(self) -> None:
        result = resolve(Evidence(order_ref="8842"))
        assert result.outcome is Outcome.AMBIGUOUS
        assert result.payment is None

    def test_both_candidates_are_recorded(self) -> None:
        # S11 requires both to appear in the ledger. A reviewer needs to see
        # what the agent weighed, not only what it chose.
        result = resolve(Evidence(order_ref="8842"))
        assert {c.payment_id for c in result.candidates} == {"pay_8842H", "pay_8842J"}
        assert len(result.to_payload()["candidates"]) == 2


class TestEvidenceDecides:
    def test_an_amount_matching_the_older_payment_selects_it(self) -> None:
        # The assertion that matters most. Recency would pick the newer one,
        # and the merchant named an amount that belongs to the older — a
        # refund against the wrong payment is exactly the S11 failure.
        result = resolve(Evidence(order_ref="8842", amount=Money(120000, "INR")))
        assert result.outcome is Outcome.RESOLVED
        assert result.payment is not None
        assert result.payment["payment_id"] == "pay_8842H"
        assert "amount" in result.reason

    def test_an_amount_matching_the_newer_payment_selects_it(self) -> None:
        result = resolve(Evidence(order_ref="8842", amount=Money(340000, "INR")))
        assert result.payment is not None
        assert result.payment["payment_id"] == "pay_8842J"

    def test_a_card_last_four_selects_the_payment_that_carries_it(self) -> None:
        result = resolve(Evidence(order_ref="8842", buyer_last_four="5512"))
        assert result.payment is not None
        assert result.payment["payment_id"] == "pay_8842H"

    def test_evidence_matching_neither_candidate_stays_ambiguous(self) -> None:
        # Supplying a fact that corroborates nothing must not license a guess.
        result = resolve(Evidence(order_ref="8842", amount=Money(99900, "INR")))
        assert result.outcome is Outcome.AMBIGUOUS

    def test_recency_cannot_outvote_a_corroborating_fact(self) -> None:
        # Recency is a tie-breaker worth a fraction of one fact. If it could
        # outweigh an amount match, the whole scheme collapses back into
        # "pick the most recent".
        from packages.agent.resolution import score_candidate

        with_amount = score_candidate(
            OLDER, Evidence(order_ref="8842", amount=Money(120000, "INR")), NOW
        )
        newest_without = score_candidate(NEWER, Evidence(order_ref="8842"), NOW)
        assert with_amount.score > newest_without.score

    def test_a_narrow_margin_asks_rather_than_guesses(self) -> None:
        # Two payments both matching on date, differing only by recency.
        same_day_a = {**OLDER, "payment_id": "pay_A", "created_at": "2026-10-10T09:00:00Z"}
        same_day_b = {**NEWER, "payment_id": "pay_B", "created_at": "2026-10-10T17:00:00Z"}
        result = resolve(
            Evidence(order_ref="8842", around_date="2026-10-10T12:00:00Z"),
            payments=[same_day_a, same_day_b],
        )
        assert result.outcome is Outcome.AMBIGUOUS
        margin = result.candidates[0].score - result.candidates[1].score
        assert margin < DECISIVE_MARGIN


class TestClarifyingQuestion:
    def test_it_offers_the_distinguishing_facts(self) -> None:
        # S11's `should`: answerable in one line, without asking the merchant
        # to restate their request.
        question = clarifying_question(resolve(Evidence(order_ref="8842")))
        assert "3,400.00" in question
        assert "1,200.00" in question
        assert "2026-10-10" in question
        assert question.count("?") == 1

    def test_it_does_not_ask_the_merchant_to_repeat_themselves(self) -> None:
        question = clarifying_question(resolve(Evidence(order_ref="8842"))).lower()
        for phrasing in ("could you clarify", "please provide more", "resend", "rephrase"):
            assert phrasing not in question


class TestExtraction:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("please refund ₹3,400", 340000),
            ("refund Rs. 1,200.50 to the buyer", 120050),
            ("the INR 999 charge", 99900),
            ("no amount mentioned here", None),
        ],
    )
    def test_amount_extraction(self, text: str, expected: int | None) -> None:
        amount = parse_amount(text)
        assert (amount.amount_minor if amount else None) == expected

    def test_an_absent_amount_is_none_not_a_guess(self) -> None:
        # An amount the merchant did not write is not evidence, and inventing
        # one would corroborate a candidate on nothing.
        assert parse_amount("there is a problem with order 8842") is None

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("card ending 5512", "5512"),
            ("last 4 1234", "1234"),
            ("order 8842 needs a refund", None),
        ],
    )
    def test_last_four_extraction(self, text: str, expected: str | None) -> None:
        assert parse_last_four(text) == expected
