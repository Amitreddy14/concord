"""Verifying a proposed action against the facts it claims to rest on.

Distinct from ``grounding.py``, which checks a *reply* against passages. This
checks a *proposal* against the systems of record, and the difference matters:
a reply that cites badly is embarrassing, while a proposal that rests on a
stale read moves money.

The check re-reads the facts rather than trusting state. State was written by
a specialist some seconds ago, and in those seconds a refund may have been
issued by a human, a capture may have been disputed, or the merchant's tier
may have changed. Trusting the earlier read is how an agent refunds a capture
that is no longer refundable — a race that will never show up in a test that
writes state and immediately reads it back.

What this does not do is decide whether the action is *permitted*. That is the
policy engine's job in phase 6, over tenant configuration. This establishes
that the facts the proposal asserts are true now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from packages.shared.money import Money
from packages.tools.errors import ToolError


class VerificationVerdict(StrEnum):
    CONFIRMED = "confirmed"
    """Every fact the proposal rests on is true as of now."""

    STALE = "stale"
    """A fact has changed since the specialist read it."""

    UNSUPPORTED = "unsupported"
    """The proposal asserts something the systems of record do not say."""

    UNVERIFIABLE = "unverifiable"
    """A fact could not be re-read. Treated as a failure, not an absence."""


@dataclass(frozen=True, slots=True)
class VerificationResult:
    verdict: VerificationVerdict
    checked: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.verdict is VerificationVerdict.CONFIRMED

    def to_payload(self) -> dict[str, Any]:
        return {
            "verdict": str(self.verdict),
            "checked": self.checked,
            "problems": self.problems,
            "confirmed": self.confirmed,
        }


async def verify_refund_proposal(
    proposal: dict[str, Any],
    *,
    invoker: Any,
    run_id: str,
    node: str = "verify_proposal",
) -> VerificationResult:
    """Re-read the capture a refund proposal targets and check it still holds."""
    capture_id = proposal.get("capture_id")
    if not capture_id:
        return VerificationResult(
            VerificationVerdict.UNSUPPORTED,
            problems=["the proposal names no capture"],
        )

    try:
        fresh = await invoker.call(
            "get_capture", {"capture_id": capture_id}, run_id=run_id, node=node
        )
    except ToolError as error:
        # Unverifiable is a failure, not an absence. An action whose
        # preconditions could not be confirmed must not proceed on the
        # grounds that nothing said no.
        return VerificationResult(
            VerificationVerdict.UNVERIFIABLE,
            problems=[f"could not re-read capture {capture_id}: {error.failure.code}"],
        )

    capture = fresh.data
    checked = [f"capture {capture_id} re-read"]
    problems: list[str] = []

    if not capture.get("refundable"):
        problems.append(
            f"capture {capture_id} is no longer refundable; "
            "it may have been fully refunded or disputed since it was read"
        )

    try:
        requested = Money.from_payload(proposal["amount"])
        captured = Money.from_payload(capture["amount"])
        already = Money.from_payload(capture["refunded_amount"])
    except (KeyError, ValueError) as error:
        return VerificationResult(
            VerificationVerdict.UNSUPPORTED,
            checked=checked,
            problems=[f"amounts could not be compared: {error}"],
        )

    remaining = captured - already
    checked.append(f"unrefunded remainder is {remaining.format()}")

    if requested > remaining:
        problems.append(
            f"the proposed {requested.format()} exceeds the unrefunded remainder "
            f"{remaining.format()}"
        )

    # The specialist's own reading, compared against the fresh one. A mismatch
    # means something changed between the two, which is worth naming
    # distinctly from a proposal that was wrong to begin with.
    observed = proposal.get("observed_remainder")
    if observed is not None:
        try:
            if Money.from_payload(observed) != remaining:
                return VerificationResult(
                    VerificationVerdict.STALE,
                    checked=checked,
                    problems=[
                        f"the remainder was {Money.from_payload(observed).format()} when read "
                        f"and is {remaining.format()} now"
                    ],
                )
        except ValueError:
            problems.append("the recorded remainder could not be parsed")

    if problems:
        return VerificationResult(
            VerificationVerdict.UNSUPPORTED, checked=checked, problems=problems
        )
    return VerificationResult(VerificationVerdict.CONFIRMED, checked=checked)
