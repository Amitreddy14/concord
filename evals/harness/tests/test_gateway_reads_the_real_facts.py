"""The double's patterns must match what the nodes actually emit.

`FACT_PATTERNS` has broken twice, both times silently and both times the same
way: the pattern owns a format that `specialist_facts` owns. First it wanted a
colon where the facts had none, then `\\S+?` captured "17" out of "expected 17
October 2026". Neither failed in the pattern — each failed later, as a reply
missing a date, in a scenario that reads as the agent's bug rather than the
harness's.

So the patterns are run here against a facts block built by the real function.
The expected values are the values put into the state, rendered the way the
system renders them for a prompt — not strings copied out of the patterns.
"""

from __future__ import annotations

from evals.harness.gateway import FACT_PATTERNS
from packages.agent.specialists import specialist_facts
from packages.shared.dates import format_day

SETTLEMENT_DUE = "2026-10-17T10:00:00Z"
EVIDENCE_DUE = "2026-10-26T23:59:59Z"

STATE = {
    "settlements": [
        {
            "settlement_id": "sett_1",
            "status": "processing",
            "expected_at": SETTLEMENT_DUE,
            "utr": None,
        }
    ],
    "evidence_packet": {
        "chargebacks": [
            {
                "chargeback_id": "cb_1",
                "reason_code": "4853",
                "evidence_due_at": EVIDENCE_DUE,
            }
        ]
    },
}


def _extract(name: str) -> str | None:
    facts = specialist_facts(STATE)  # type: ignore[arg-type]
    match = FACT_PATTERNS[name].search(facts)
    return match.group(1) if match else None


def test_the_settlement_status_is_extracted() -> None:
    assert _extract("settlement_status") == "processing"


def test_the_whole_expected_date_is_extracted() -> None:
    """Whole, not truncated at the first space.

    This is the assertion that was missing. A pattern capturing "17" still
    matches, still produces a reply, and still fails a scenario three layers
    away from the cause.
    """
    assert _extract("expected_at") == format_day(SETTLEMENT_DUE)


def test_the_whole_evidence_deadline_is_extracted() -> None:
    assert _extract("evidence_due") == format_day(EVIDENCE_DUE)


def test_every_pattern_finds_something_in_a_populated_facts_block() -> None:
    """Totality, in the project's usual sense: enumerate, do not sample.

    A pattern that matches nothing is the failure mode both regressions had,
    and a new pattern added without a fact to match is the next one.
    """
    state = {
        **STATE,
        "refund": {"refund_id": "rfnd_1", "amount": {"minor": 199900, "currency": "INR"}},
    }
    from packages.agent.nodes import draft_facts

    facts = draft_facts(state)  # type: ignore[arg-type]
    unmatched = [name for name, pattern in FACT_PATTERNS.items() if not pattern.search(facts)]
    assert not unmatched, f"these patterns match nothing in a populated facts block: {unmatched}"
