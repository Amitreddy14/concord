"""A narrated walkthrough of four scenarios, for a person watching.

`make eval` answers "does it pass"; this answers "what does it do". The four
scenarios are chosen because together they are the argument for the
architecture rather than a feature tour:

    S02  it acts on its own when policy allows
    S04  it stops when policy does not, and survives the wait
    S09  it does not pay twice when the gateway times out mid-refund
    S10  it cannot be talked into widening its own authority

Runs against the deterministic stand-in by default, so a reviewer can watch
it without a GPU; `--model qwen3:8b` substitutes a real model and nothing
else. Everything shown is read from the run's own state and stores — there is
no narration here that the system did not produce.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import textwrap
from typing import Any

from evals.fixtures import Scenario, load_all
from evals.harness.run import RunReport, run_scenario, run_scenario_with_model

DEMO_ORDER = ("S02", "S04", "S09", "S10")

#: The question each scenario answers, in the terms someone evaluating this
#: system would ask it. Written here rather than taken from the fixture's
#: `exercises` list, which names mechanisms (`durable_pause`) where a reader
#: wants consequences ("does the approval survive a restart").
QUESTIONS = {
    "S02": "Does it act on its own when the policy allows it — and refund the right capture?",
    "S04": "Does it stop when the policy does not allow it, and survive the wait for a human?",
    "S09": "When the gateway times out mid-refund, does the merchant get refunded exactly once?",
    "S10": "Can a merchant's own ticket text talk it into doing something it may not do?",
}

RULE = "─" * 78


WIDTH = 78
LABEL = 9
"""Width of the label column, so continuation lines align under the text."""


def _field(label: str, text: str) -> str:
    """One labelled line, wrapped with its continuations aligned.

    An empty label gives a continuation of the field above it, which is how a
    decision's reasons sit under the decision.
    """
    prefix = f"    {label:<{LABEL}} "
    return textwrap.fill(
        " ".join(str(text).split()) or "—",
        width=WIDTH,
        initial_indent=prefix,
        subsequent_indent=" " * len(prefix),
    )


def _note(text: str, indent: str = "  ") -> str:
    return textwrap.fill(
        " ".join(text.split()), width=WIDTH, initial_indent=indent, subsequent_indent=indent
    )


def _ticket_block(scenario: Scenario) -> list[str]:
    ticket = scenario.ticket
    return [
        "  the ticket",
        _field("merchant", str(ticket.get("merchant_id", "unknown"))),
        _field("subject", str(ticket.get("subject", ""))),
        _field("body", str(ticket.get("body", ""))),
    ]


def _decision_block(diagnostics: dict[str, Any]) -> list[str]:
    permission = diagnostics.get("permission")
    if not permission:
        return [_field("decision", "no action was proposed, so nothing needed deciding")]

    tier = diagnostics.get("risk_tier")
    rule = diagnostics.get("rule_id")
    lines = [_field("decision", f"{permission}  ·  tier {tier}")]
    if rule and rule != tier:
        # The two answer different questions: the tier classifies the request,
        # the rule names what decided it. They differ exactly when something
        # outranked the tier — a forbidden action, or the state of the world.
        lines.append(_field("", f"decided by {rule}"))
    for reason in diagnostics.get("decision_reasons") or []:
        lines.append(_field("", reason))
    return lines


def _run_block(report: RunReport) -> list[str]:
    d = report.diagnostics or {}
    passed = sum(1 for c in report.checks if c.ok)
    lines = [f"  {report.label}"]

    if report.error:
        lines.append(f"    ERROR     {report.error}")
        return lines

    lines.append(_field("intent", str(d.get("intent"))))
    lines.append(_field("path", " -> ".join(d.get("path") or [])))
    lines.extend(_decision_block(d))

    refunds = d.get("refunds") or 0
    if refunds:
        lines.append(_field("money", f"{refunds} refund(s) issued"))
    assignee = d.get("ticket_assignee") or "unassigned"
    lines.append(_field("ticket", f"{d.get('ticket_status')} · {assignee}"))

    reply = (d.get("reply_excerpt") or "").strip()
    if reply:
        lines.append(_field("refusal" if d.get("reply_is_refusal") else "reply", reply))
        cited = d.get("draft_citations") or d.get("reply_citations") or []
        if cited:
            lines.append(_field("", f"citing {', '.join(cited)}"))
    else:
        lines.append(_field("reply", "nothing was sent to the merchant"))

    lines.append(_field("checks", f"{passed}/{len(report.checks)}"))
    return lines


def _scenario_summary(scenario_id: str, reports: list[RunReport]) -> list[str]:
    """What the whole scenario proved, where one run cannot show it.

    S09's point is only visible across its five repeats, and S04's only
    across its four branches. A per-run block cannot say either.
    """
    if scenario_id == "S09":
        totals = {(r.diagnostics or {}).get("refunds", 0) for r in reports}
        return [
            "",
            f"  across {len(reports)} runs of the same ticket, each with the gateway timing out "
            f"mid-refund, the number of refunds on the capture was {sorted(totals)} — "
            "the derived idempotency key is what makes a retry safe rather than hopeful.",
        ]
    if scenario_id == "S04":
        branches = [r.label.replace("branch: ", "") for r in reports if "branch" in r.label]
        return [
            "",
            f"  the run paused and was resumed four ways — {', '.join(branches)} — from the "
            "same checkpoint. `expired` is the one worth watching: nobody acted, and the "
            "run abandoned the proposal and escalated rather than waiting forever.",
        ]
    if scenario_id == "S10":
        d = reports[0].diagnostics or {}
        scopes = sorted(d.get("scopes_used") or [])
        reasons = " ".join(d.get("decision_reasons") or [])
        lines = [
            "",
            "the ticket claims the merchant is verified enterprise tier, and asks for every "
            "capture refunded without review.",
        ]
        if "standard-tier" in reasons:
            # The sharpest moment in the walkthrough, and worth stating rather
            # than leaving a reader to notice: the engine priced the cap off
            # the CRM's tier, not the ticket's claim about it.
            lines.append(
                "the cap it was measured against is the standard-tier one. The merchant's "
                "tier was read from the CRM, because an identifier or an attribute arriving "
                "in merchant text is re-validated against the system of record and never "
                "trusted as given (ADR 0005, rule 3)."
            )
        lines.append(
            f"the scopes the run used: {', '.join(scopes) or 'none'}. Nothing derived from a "
            "ticket can widen that set — scope comes from tenant configuration, and the "
            "injection's success or failure at confusing the model does not change what a "
            "confused model is able to do (ADR 0005, rule 4)."
        )
        return lines
    return []


async def _reports(scenario: Scenario, model: str | None, base_url: str) -> list[RunReport]:
    if model:
        return await run_scenario_with_model(scenario, model=model, base_url=base_url)
    return await run_scenario(scenario)


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Narrated walkthrough of four scenarios.")
    parser.add_argument(
        "--model",
        default=None,
        help="Run against this Ollama model instead of the deterministic stand-in.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
    )
    parser.add_argument("--scenarios", default=",".join(DEMO_ORDER))
    args = parser.parse_args(argv)

    catalogue = load_all()
    wanted = [s.strip().upper() for s in args.scenarios.split(",") if s.strip()]
    missing = [s for s in wanted if s not in catalogue]
    if missing:
        print(f"no such scenario: {', '.join(missing)}")
        return 2

    where = f"model {args.model}" if args.model else "the deterministic stand-in"
    noun = "scenario" if len(wanted) == 1 else "scenarios"
    print(f"\nConcord — {len(wanted)} {noun}, against {where}.\n")

    failures = 0
    for scenario_id in wanted:
        scenario = catalogue[scenario_id]
        print(RULE)
        print(f"{scenario_id}  {scenario.title}")
        print(_note(QUESTIONS.get(scenario_id, ""), indent="    "))
        print(RULE)
        print()
        print("\n".join(_ticket_block(scenario)))
        print()

        reports = await _reports(scenario, args.model, args.base_url)
        for report in reports:
            print("\n".join(_run_block(report)))
            print()
            failures += sum(1 for c in report.checks if not c.ok) + bool(report.error)

        paragraphs = [p for p in _scenario_summary(scenario_id, reports) if p.strip()]
        for paragraph in paragraphs:
            print(_note(paragraph))
            print()

    print(RULE)
    if failures:
        print(f"{failures} check(s) failed. The walkthrough above is of a system that is broken.")
        return 1
    noun = "scenario" if len(wanted) == 1 else "scenarios"
    print(f"Every assertion in all {len(wanted)} {noun} passed.")
    print("`make eval` runs all twelve; `make benchmark` compares models.")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
