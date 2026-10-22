"""Entity resolution: deciding which payment a ticket is about.

The hard part is stated in the domain model: ``order_ref`` is the merchant's
own identifier and carries no uniqueness guarantee. A merchant can
legitimately have two payments with ``order_ref = "8842"`` months apart, and
S11 exists because resolving that by taking the most recent one produces a
plausible refund against the wrong payment.

Everything here is a pure function over facts already read from the systems of
record. No model call, because "which of these two payments did they mean" is
a judgement about evidence, and a judgement about evidence is testable —
unlike a judgement produced by sampling.

The resolution order is the domain model's, implemented literally:

1. scope every lookup to the authenticated merchant;
2. match ``order_ref`` within that merchant;
3. one match: proceed;
4. several: rank by corroborating facts, and proceed only if the top candidate
   is unambiguous;
5. none: say so rather than offering a near-match.

Step 4 is where the care goes. "Unambiguous" is not "ranked first" — a
candidate that merely edges out another is exactly the case where asking
costs one message and guessing costs a wrong refund.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from packages.shared.money import Money

# Scores are integer points, not floats. A corroborating fact is worth 100,
# so the arithmetic is exact and a margin comparison cannot turn on a
# floating-point representation — the same reason money is integer minor
# units, applied where it also happens to help.
FACT_POINTS = 100
NEAR_DATE_POINTS = 50
MAX_RECENCY_POINTS = 25

# How far ahead of the runner-up a candidate must be to resolve without
# asking. A third of a corroborating fact: enough that a recency edge alone
# never decides, since recency can contribute at most a quarter.
DECISIVE_MARGIN = 34

# A capture within this many days of a stated date is a match; within the
# wider window it is weak corroboration rather than none.
EXACT_DATE_DAYS = 2
NEAR_DATE_DAYS = 14
RECENCY_HORIZON_DAYS = 365

AMOUNT_PATTERN = re.compile(r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE)
LAST_FOUR_PATTERN = re.compile(r"\b(?:ending|last\s*4|card)\s*(?:in\s*)?(\d{4})\b", re.IGNORECASE)


class Outcome(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class Evidence:
    """Facts from the ticket that can corroborate a candidate.

    Extracted by the model and then checked here against the systems of
    record, never trusted as given — ADR 0005 rule 3.
    """

    order_ref: str | None = None
    amount: Money | None = None
    buyer_last_four: str | None = None
    around_date: str | None = None

    @property
    def corroborating_count(self) -> int:
        return sum(
            1 for v in (self.amount, self.buyer_last_four, self.around_date) if v is not None
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    payment: dict[str, Any]
    score: int
    matched_on: list[str] = field(default_factory=list)

    @property
    def payment_id(self) -> str:
        return str(self.payment["payment_id"])


@dataclass(frozen=True, slots=True)
class Resolution:
    outcome: Outcome
    payment: dict[str, Any] | None = None
    candidates: list[Candidate] = field(default_factory=list)
    reason: str = ""

    @property
    def is_resolved(self) -> bool:
        return self.outcome is Outcome.RESOLVED

    def to_payload(self) -> dict[str, Any]:
        return {
            "outcome": str(self.outcome),
            "payment_id": self.payment["payment_id"] if self.payment else None,
            # Every candidate considered is recorded, not only the winner.
            # S11 asserts both appear, because a reviewer needs to see what
            # the agent weighed, not just what it chose.
            "candidates": [
                {"payment_id": c.payment_id, "score": c.score, "matched_on": c.matched_on}
                for c in self.candidates
            ],
            "reason": self.reason,
        }


def parse_amount(text: str, currency: str = "INR") -> Money | None:
    """Pull a monetary amount out of merchant text, if it states one.

    Returns None rather than guessing. An amount the merchant did not write is
    not evidence, and inventing one would corroborate a candidate on nothing.
    """
    match = AMOUNT_PATTERN.search(text)
    if match is None:
        return None
    digits = match.group(1).replace(",", "")
    if "." in digits:
        units, _, minor = digits.partition(".")
        minor = (minor + "00")[:2]
    else:
        units, minor = digits, "00"
    try:
        return Money(int(units) * 100 + int(minor), currency)
    except (ValueError, TypeError):
        return None


def parse_last_four(text: str) -> str | None:
    match = LAST_FOUR_PATTERN.search(text)
    return match.group(1) if match else None


# Returned when two timestamps cannot be compared at all. A sentinel rather
# than an exception, because an unparseable date is missing evidence rather
# than a failure, and a large integer rather than infinity because this module
# holds no floats.
INCOMPARABLE_DAYS = 10**6


def _days_between(a: str, b: str) -> int:
    """Whole days between two timestamps, rounded down."""
    try:
        first = datetime.fromisoformat(a.replace("Z", "+00:00"))
        second = datetime.fromisoformat(b.replace("Z", "+00:00"))
    except ValueError:
        return INCOMPARABLE_DAYS
    return int(abs((first - second).total_seconds())) // 86400


def score_candidate(payment: dict[str, Any], evidence: Evidence, now: datetime) -> Candidate:
    """Score one candidate against the evidence the ticket supplied.

    Recency is a tie-breaker worth a fraction of one corroborating fact, not a
    criterion. A ticket that names an amount matching the older payment is
    about the older payment, and letting recency outweigh that is precisely
    the S11 failure.
    """
    score = 0
    matched: list[str] = []

    if evidence.amount is not None:
        try:
            if Money.from_payload(payment["amount"]) == evidence.amount:
                score += FACT_POINTS
                matched.append("amount")
        except (KeyError, ValueError):
            pass

    if (
        evidence.buyer_last_four is not None
        and payment.get("buyer_reference")
        and str(payment["buyer_reference"]) == evidence.buyer_last_four
    ):
        score += FACT_POINTS
        matched.append("buyer_reference")

    if evidence.around_date is not None and payment.get("created_at"):
        gap = _days_between(str(payment["created_at"]), evidence.around_date)
        if gap <= EXACT_DATE_DAYS:
            score += FACT_POINTS
            matched.append("date")
        elif gap <= NEAR_DATE_DAYS:
            score += NEAR_DATE_POINTS
            matched.append("date_approx")

    # Recency, bounded so it can never outvote a corroborating fact: at most
    # MAX_RECENCY_POINTS against a fact's FACT_POINTS.
    created = str(payment.get("created_at", ""))
    age_days = _days_between(created, now.isoformat()) if created else INCOMPARABLE_DAYS
    if age_days < INCOMPARABLE_DAYS:
        capped = min(age_days, RECENCY_HORIZON_DAYS)
        score += MAX_RECENCY_POINTS * (RECENCY_HORIZON_DAYS - capped) // RECENCY_HORIZON_DAYS

    return Candidate(payment=payment, score=score, matched_on=matched)


def resolve_payment(
    *,
    payments: list[dict[str, Any]],
    evidence: Evidence,
    merchant_id: str,
    now: datetime | None = None,
) -> Resolution:
    """Pick the payment a ticket refers to, or decline to."""
    now = now or datetime.now(UTC)

    # Step 1. Never resolve across merchants, whatever the ticket claims.
    in_scope = [p for p in payments if p.get("merchant_id") == merchant_id]
    if not in_scope:
        return Resolution(
            Outcome.NOT_FOUND,
            reason=f"no payment for merchant {merchant_id} matches this ticket",
        )

    # Step 2.
    if evidence.order_ref is not None:
        matching = [p for p in in_scope if str(p.get("order_ref")) == str(evidence.order_ref)]
        if not matching:
            return Resolution(
                Outcome.NOT_FOUND,
                reason=(
                    f"merchant {merchant_id} has no payment with order reference "
                    f"{evidence.order_ref!r}"
                ),
            )
    else:
        matching = in_scope

    # Step 3.
    if len(matching) == 1:
        only = score_candidate(matching[0], evidence, now)
        return Resolution(
            Outcome.RESOLVED, payment=matching[0], candidates=[only], reason="one match"
        )

    # Step 4.
    ranked = sorted(
        (score_candidate(p, evidence, now) for p in matching),
        key=lambda c: (-c.score, c.payment_id),
    )
    best, runner_up = ranked[0], ranked[1]

    if evidence.corroborating_count == 0:
        return Resolution(
            Outcome.AMBIGUOUS,
            candidates=ranked,
            reason=(
                f"{len(ranked)} payments share order reference {evidence.order_ref!r} and the "
                "ticket gives nothing to tell them apart"
            ),
        )

    if not best.matched_on:
        return Resolution(
            Outcome.AMBIGUOUS,
            candidates=ranked,
            reason="no candidate matches any fact the ticket supplied",
        )

    margin = best.score - runner_up.score
    if margin < DECISIVE_MARGIN:
        return Resolution(
            Outcome.AMBIGUOUS,
            candidates=ranked,
            reason=(
                f"the two leading candidates are within {margin} points of each other; "
                "asking costs one message and guessing costs a wrong refund"
            ),
        )

    return Resolution(
        Outcome.RESOLVED,
        payment=best.payment,
        candidates=ranked,
        reason=f"matched on {', '.join(best.matched_on)}",
    )


def clarifying_question(resolution: Resolution) -> str:
    """What to ask when the evidence does not decide.

    Offers the facts that distinguish the candidates rather than asking the
    merchant to restate their request — S11's `should` requires it be
    answerable in one line.
    """
    lines = []
    for candidate in resolution.candidates[:3]:
        payment = candidate.payment
        amount = Money.from_payload(payment["amount"]).format()
        when = str(payment.get("created_at", ""))[:10]
        method = payment.get("method", "")
        lines.append(f"{amount} on {when} by {method}")

    options = "; or ".join(lines)
    return (
        f"I can see more than one payment under that order reference — {options}. "
        "Which one should I look at?"
    )
