"""The model stand-in the harness runs against by default.

A merge gate must not depend on an 8B model's mood. A suite that fails because
a sampled reply reworded a sentence is a suite people disable, and a disabled
suite catches nothing — so the hard assertions run against a stand-in that
behaves the way a competent model would, and the real model is measured
separately by `make benchmark`.

What this is *not* is a canned answer per scenario. Hardcoding a draft's
citations means hardcoding the passage ids retrieval happens to return, which
breaks the first time the corpus or the ranking changes — a lesson learned the
expensive way, with a scenario whose scripted draft cited a passage retrieval
no longer produced and which therefore failed grounding for a reason that had
nothing to do with the scenario.

So this gateway reads the prompt. For a draft it cites the passages it was
actually given, exactly as a model that follows instructions would, and fills
the reply from the facts the node put in front of it. That makes it a test of
the graph — did the right facts reach the drafter, did grounding accept them,
did the reply get posted — which is what a merge gate should be testing.

Triage is scripted per scenario, because an intent is a fact about the ticket
rather than something to derive from the prompt, and a stand-in that guessed
it would be testing itself.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from packages.shared.llm import Completion, UsageLedger, estimate_tokens

#: What triage returns per scenario. Facts about the ticket, stated once.
TRIAGE: dict[str, dict[str, Any]] = {
    "S01": {"intent": "status_question", "summary": "Asks how long refunds take."},
    "S02": {
        "intent": "refund_request",
        "order_ref": "8842",
        "summary": "Reports being charged twice for one order.",
    },
    "S03": {
        "intent": "refund_request",
        "order_ref": "9120",
        "summary": "Asks for a refund on a single order.",
    },
    "S04": {
        "intent": "refund_request",
        "order_ref": "7731",
        "summary": "Reports a duplicated order and asks for the full amount back.",
    },
    "S05": {
        "intent": "refund_request",
        "order_ref": "4410",
        "summary": "Asks for a refund on an old order.",
    },
    "S06": {
        "intent": "dispute_notice",
        "order_ref": "6620",
        "summary": "Forwards a chargeback notice from the issuer.",
    },
    "S07": {
        "intent": "settlement_query",
        "summary": "Asks why a settlement has not arrived.",
    },
    "S08": {
        "intent": "payout_hold_query",
        "summary": "Asks why payouts are held and wants them released.",
    },
    "S09": {
        "intent": "refund_request",
        "order_ref": "5150",
        "summary": "Asks for a refund on order 5150.",
    },
    "S10": {
        "intent": "refund_request",
        "order_ref": "3301",
        "suspected_injection": True,
        "summary": "Asks for a refund; the body imitates system instructions.",
    },
    "S11": {
        "intent": "refund_request",
        "order_ref": "8842",
        "summary": "Asks for a refund on order 8842 without saying which one.",
    },
    "S12": {
        "intent": "complaint_no_action",
        "summary": "Complains about repeated contact with no resolution.",
    },
}

TRIAGE_DEFAULTS: dict[str, Any] = {
    "intent": "unclear",
    "order_ref": None,
    "mentions_amount": False,
    "urgency_claimed": False,
    "suspected_injection": False,
    "summary": "No summary.",
}

#: Passage ids the drafter was given, pulled out of the facts block the node
#: assembled. The format is `<passage_id>: <title> — <text>`.
PASSAGE_LINE = re.compile(r"^(psg_[\w.\-]+):", re.MULTILINE)

#: A `run:` citation the node offered for something this run performed.
RUN_CITATION = re.compile(r"^(run:[\w.:\-]+):", re.MULTILINE)

#: Values the drafter was handed, so the reply can state them rather than
#: inventing them. Each is a fact a scenario's assertions ask the reply to
#: carry, and each comes out of the prompt rather than out of this file.
#: Matched against the exact strings `specialist_facts` emits, not against a
#: format invented here. Writing these from memory produced patterns that
#: matched nothing — the facts say "evidence due 2026-10-26T…" and the
#: pattern wanted a colon — so a reply silently omitted a date the node had
#: handed it, and the scenario failed on the reply rather than on the gap.
#:
#: They broke a second time when dates began arriving rendered: `\S+?`
#: captured "17" out of "expected 17 October 2026" and the reply stated a
#: number. A value that may contain spaces needs an anchor at its far end —
#: the comma that follows it, or the end of the line — not a non-whitespace
#: run. The eval suite caught both, which is the point of a double that
#: reads the prompt instead of the scenario id.
FACT_PATTERNS: dict[str, re.Pattern[str]] = {
    # settlement sett_x: processing, expected 17 October 2026, utr …
    "settlement_status": re.compile(r"^settlement \S+:\s*(\w+),", re.MULTILINE),
    "expected_at": re.compile(r"expected (.+?),", re.IGNORECASE),
    # chargeback cb_x: 4853, evidence due 26 October 2026
    "evidence_due": re.compile(r"evidence due (.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    # run:refund:rfnd_x: a refund of INR 1,999.00 has been issued against …
    "refund_amount": re.compile(r"a refund of (.+?) has been issued"),
}


@dataclass
class HarnessGateway:
    """Deterministic, prompt-aware, and honest about which it is.

    Presents the same surface as `OllamaGateway` for the two nodes that call
    a model. Everything it returns is derived from the scenario id and the
    prompt it was handed, so a run is reproducible and a changed prompt
    changes the answer — which is the property a stand-in needs if the suite
    is to mean anything.
    """

    scenario_id: str
    model: str = "harness"
    usage: UsageLedger = field(default_factory=UsageLedger)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete(
        self,
        *,
        system: str,
        user: str,
        node: str,
        run_id: str,
        budget_tokens: int,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,  # allow-float: the gateway protocol's sampling knob
        max_tokens: int = 1024,
    ) -> Completion:
        self.calls.append({"node": node, "run_id": run_id})

        if node == "triage":
            text = json.dumps({**TRIAGE_DEFAULTS, **TRIAGE.get(self.scenario_id, {})})
        elif node == "draft":
            text = json.dumps(self._draft(system, user))
        else:
            raise AssertionError(f"the harness gateway was asked for an unscripted node: {node!r}")

        completion = Completion(
            text=text,
            model=self.model,
            prompt_tokens=estimate_tokens(system) + estimate_tokens(user),
            completion_tokens=estimate_tokens(text),
            # Zero rather than a plausible-looking number. Latency against a
            # stand-in is not latency, and a report that mixed the two would
            # invite someone to read it as a benchmark.
            latency_ms=0,
        )
        self.usage.record(run_id=run_id, node=node, completion=completion)
        return completion

    async def health(self) -> dict[str, Any]:
        return {"model": self.model, "supports_tool_calling": True}

    # -- drafting --------------------------------------------------------

    def _draft(self, system: str, user: str) -> dict[str, Any]:
        prompt = f"{system}\n{user}"
        passages = PASSAGE_LINE.findall(prompt)
        run_citations = RUN_CITATION.findall(prompt)
        facts = {
            name: match.group(1)
            for name, pattern in FACT_PATTERNS.items()
            if (match := pattern.search(prompt))
        }

        sentences = [_opening(self.scenario_id, facts)]
        if run_citations:
            # The run did something. A reply that does not say so is the
            # failure S09 exists to catch.
            sentences.append(_refund_sentence(facts))
        sentences.append(_closing(self.scenario_id, facts))

        return {
            "reply": " ".join(s for s in sentences if s),
            # Cite what was given, the way a model following its instructions
            # would. Never an id that is not in the prompt.
            "citations": [*passages, *run_citations],
            "unsupported_claims": [],
        }


def _refund_sentence(facts: dict[str, str]) -> str:
    amount = facts.get("refund_amount")
    return (
        f"I have refunded {amount} to the original payment method."
        if amount
        else "I have put the refund through to the original payment method."
    )


def _opening(scenario_id: str, facts: dict[str, str]) -> str:
    if scenario_id == "S07":
        status = facts.get("settlement_status", "being processed")
        # Stated as handed over, not sliced. `expected[:10]` used to mean
        # "the date part of an ISO timestamp"; once the facts block began
        # rendering days, the same slice cut "17 October 2026" down to "17
        # October" and quietly dropped the year. A magic number standing in
        # for a format is a format assumption with nowhere to fail loudly.
        expected = facts.get("expected_at")
        when = f" and is expected on {expected}" if expected else ""
        return f"Your settlement is currently {status}{when}."
    if scenario_id == "S08":
        return (
            "Your payouts are held pending a compliance review. I am not able to "
            "release them from here."
        )
    if scenario_id == "S06":
        due = facts.get("evidence_due")
        deadline = f" Evidence is due by {due}." if due else ""
        return f"I have put together the evidence for this chargeback.{deadline}"
    if scenario_id == "S12":
        return "I can see you have been in touch about this several times."
    if scenario_id == "S05":
        return "This payment is outside the window in which a refund can be initiated."
    return "Thanks for getting in touch."


def _closing(scenario_id: str, facts: dict[str, str]) -> str:
    if scenario_id in {"S06", "S07", "S08", "S12"}:
        return "I have passed this to the team that handles it so they can take it from here."
    if scenario_id == "S05":
        return "I have passed this to a colleague who can look at what else is possible."
    if scenario_id == "S01":
        return "Card refunds reach the cardholder in 5 to 7 working days."
    return "Let me know if anything is unclear."
