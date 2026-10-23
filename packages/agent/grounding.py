"""Citation enforcement and the refusal path.

A drafted reply is not a reply until its claims are supported. This module is
what stands between the two.

The enforcement is deliberately not "ask the model whether it was grounded".
A model that invented a claim will cheerfully certify it, and asking the same
model to check its own work converts a verifiable property into a second
opinion from the same source. These checks are structural:

* every cited id must be a passage that was actually retrieved for this run;
* every cited passage's content hash must match what was retrieved, so a
  citation cannot point at text that has since been reworded;
* a reply containing claims the drafter itself could not ground does not go
  out, whatever the rest of it says;
* when the tenant requires citations and there are none, the reply is a
  refusal regardless of how reasonable it sounds.

What a model *is* good at — judging whether a specific sentence is entailed by
a specific passage — belongs in phase 8's graded evaluation, not in a gate
that blocks a send. The gate checks facts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class GroundingVerdict(StrEnum):
    SUPPORTED = "supported"
    """Every claim traces to a retrieved passage. The reply may be sent."""

    UNSUPPORTED = "unsupported"
    """The corpus does not support an answer. The reply is a refusal."""

    INVALID_CITATION = "invalid_citation"
    """A citation names a passage that was not retrieved, or has changed."""

    PARTIAL = "partial"
    """The drafter flagged claims it could not ground."""


@dataclass(frozen=True, slots=True)
class GroundingResult:
    verdict: GroundingVerdict
    citations: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def may_send(self) -> bool:
        return self.verdict is GroundingVerdict.SUPPORTED

    def to_payload(self) -> dict[str, Any]:
        return {
            "verdict": str(self.verdict),
            "citations": self.citations,
            "problems": self.problems,
            "may_send": self.may_send,
        }


#: The namespace for a claim evidenced by the run's own ledger rather than
#: by the knowledge corpus. "I have refunded ₹1,999" is not a policy claim
#: and no passage supports it; what supports it is that this run did it, and
#: the ledger records that more firmly than any corpus could.
#:
#: Without this, a reply stating what the run just did had to either cite a
#: passage that does not mention it — a fabricated reference — or go
#: uncited and be refused. S09 asks for a reply that reports its refund, and
#: both of those produce a reply that does not.
SELF_EVIDENT_PREFIX = "run:"


def verify(
    *,
    draft: dict[str, Any],
    retrieved: list[dict[str, Any]],
    require_citations: bool,
    self_evident: Sequence[str] = (),
) -> GroundingResult:
    """Decide whether a draft may be sent.

    ``self_evident`` names citations the run itself evidences — the refund it
    issued, the ticket it escalated. They are supplied by the caller from
    state rather than accepted on the model's word, so a drafter inventing a
    `run:` citation is still caught.
    """
    citations = list(draft.get("citations") or [])
    unsupported = list(draft.get("unsupported_claims") or [])
    problems: list[str] = []

    by_id = {p["passage_id"]: p for p in retrieved}
    permitted_self_evident = set(self_evident)

    # A citation naming something that was never retrieved is the clearest
    # signal of a fabricated reference: the model produced an id that looks
    # plausible for a corpus it did not see.
    unknown = [c for c in citations if c not in by_id and c not in permitted_self_evident]
    if unknown:
        problems.append(f"cited passages that were not retrieved: {', '.join(sorted(unknown))}")

    # A citation can also go stale. The passage id is structural and survives
    # rewording, which is correct for stability, but it means an id alone no
    # longer proves the text is what the drafter saw.
    stale = [
        c
        for c in citations
        if c in by_id
        and draft.get("citation_hashes", {}).get(c) not in (None, by_id[c].get("content_hash"))
    ]
    if stale:
        problems.append(f"cited passages whose text has changed: {', '.join(sorted(stale))}")

    if unsupported:
        problems.append(f"claims the drafter could not ground: {len(unsupported)}")

    if not retrieved and not permitted_self_evident:
        return GroundingResult(
            GroundingVerdict.UNSUPPORTED,
            problems=["no passage in the corpus supports an answer to this question"],
        )

    if unknown or stale:
        return GroundingResult(GroundingVerdict.INVALID_CITATION, citations, problems)

    if unsupported:
        return GroundingResult(GroundingVerdict.PARTIAL, citations, problems)

    if require_citations and not citations:
        return GroundingResult(
            GroundingVerdict.UNSUPPORTED,
            problems=["this tenant requires a cited reply and the draft cites nothing"],
        )

    return GroundingResult(GroundingVerdict.SUPPORTED, citations)


REFUSAL_TEMPLATES = {
    GroundingVerdict.UNSUPPORTED: (
        "I don't have policy that covers this, so I'm passing it to a colleague rather "
        "than guessing. They'll pick it up from here."
    ),
    GroundingVerdict.INVALID_CITATION: (
        "I couldn't verify the policy I was about to quote, so I'm passing this to a "
        "colleague rather than send something I can't stand behind."
    ),
    GroundingVerdict.PARTIAL: (
        "Part of this needs a colleague to confirm before I answer, so I'm passing it on "
        "rather than answer half of it."
    ),
}


def refusal_reply(result: GroundingResult) -> str:
    """What the merchant sees when a draft cannot be sent.

    Deliberately not an apology and deliberately not an explanation of the
    machinery. A merchant does not need to know that retrieval returned
    nothing; they need to know a person is picking it up.
    """
    return REFUSAL_TEMPLATES.get(result.verdict, REFUSAL_TEMPLATES[GroundingVerdict.UNSUPPORTED])
