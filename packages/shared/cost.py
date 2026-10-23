"""What a run cost, in the units that are actually scarce.

The deployment runs an 8B model on an 8GB card (ADR 0003), so the scarce
resources are not dollars. They are seconds a merchant waits, tokens through a
fixed-size context window, and calls against a GPU that serves one model at a
time. A cost report denominated in currency would be a report about somebody
else's deployment.

Money is included anyway, as an optional per-million-token rate, because the
question "what would this cost hosted?" is the first one anyone asks when
local inference stops being enough — and answering it from recorded tokens is
better than re-running everything against a hosted model to find out.

Two things this is careful about.

**Tool time and model time are not added together.** A run's wall clock is
neither: nodes await both, and a sum double-counts nothing but explains
nothing either. They are reported side by side, because the question is
usually which of the two dominates.

**A rate is an estimate and is labelled one.** Integer minor units throughout,
so the no-floats rule holds here as everywhere else — a cost estimate that
drifted by a rounding error would be a cost estimate nobody trusts, and the
arithmetic is the same arithmetic as a refund's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.shared.ledger import InMemoryLedger, StepKind
from packages.shared.llm import UsageLedger
from packages.shared.money import Money

#: Tokens per million, the unit hosted providers price in.
TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class Rate:
    """A hypothetical hosted price, for the "what if" question only."""

    prompt_minor_per_million: int
    completion_minor_per_million: int
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class RunCost:
    run_id: str
    model_calls: int
    prompt_tokens: int
    completion_tokens: int
    model_ms: int
    tool_calls: int
    tool_ms: int
    retries: int
    by_node: dict[str, dict[str, int]]
    estimated: Money | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def dominant(self) -> str:
        """Which half of the run's time to look at first.

        The useful answer to "why was that slow": a run dominated by tool time
        is a dependency problem, one dominated by model time is a prompt or a
        model-size problem, and they lead to different work.
        """
        if self.model_ms == self.tool_ms == 0:
            return "unknown"
        return "model" if self.model_ms >= self.tool_ms else "tools"

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "model_calls": self.model_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "model_ms": self.model_ms,
            "tool_calls": self.tool_calls,
            "tool_ms": self.tool_ms,
            "retries": self.retries,
            "dominant": self.dominant,
            "by_node": self.by_node,
            # Named `estimated_hosted_cost` rather than `cost`, because it is
            # not what this run cost. This run cost electricity.
            "estimated_hosted_cost": self.estimated.to_payload() if self.estimated else None,
        }


def for_run(
    run_id: str,
    *,
    usage: UsageLedger,
    ledger: InMemoryLedger,
    rate: Rate | None = None,
) -> RunCost:
    """Assemble a run's cost from the two ledgers that already record it.

    Nothing new is measured here. The gateway has recorded every model call
    since phase 3 and the tool layer every tool call since phase 2; this reads
    both and puts them in one shape the API and the console can show.
    """
    model_calls = [c for c in usage.calls if c["run_id"] == run_id]
    prompt_tokens = sum(int(c["prompt_tokens"]) for c in model_calls)
    completion_tokens = sum(int(c["completion_tokens"]) for c in model_calls)
    model_ms = sum(int(c["latency_ms"]) for c in model_calls)

    steps = ledger.steps(run_id)
    tool_steps = [s for s in steps if s.kind is StepKind.TOOL_CALL and s.duration_ms is not None]
    tool_ms = sum(int(s.duration_ms or 0) for s in tool_steps)

    # A retry is an attempt after the first against the same tool. Counted
    # because it is the clearest single number for how hostile the
    # dependencies were during a run.
    retries = sum(1 for s in steps if s.tool and s.attempt > 1 and str(s.outcome) != "started")

    by_node: dict[str, dict[str, int]] = {}
    for call in model_calls:
        node = str(call["node"])
        entry = by_node.setdefault(
            node, {"model_calls": 0, "tokens": 0, "model_ms": 0, "tool_ms": 0}
        )
        entry["model_calls"] += 1
        entry["tokens"] += int(call["prompt_tokens"]) + int(call["completion_tokens"])
        entry["model_ms"] += int(call["latency_ms"])
    for step in tool_steps:
        entry = by_node.setdefault(
            step.node, {"model_calls": 0, "tokens": 0, "model_ms": 0, "tool_ms": 0}
        )
        entry["tool_ms"] += int(step.duration_ms or 0)

    return RunCost(
        run_id=run_id,
        model_calls=len(model_calls),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model_ms=model_ms,
        tool_calls=len({(s.node, s.tool, s.attempt) for s in tool_steps}),
        tool_ms=tool_ms,
        retries=retries,
        by_node=dict(sorted(by_node.items())),
        estimated=_estimate(prompt_tokens, completion_tokens, rate),
    )


def _estimate(prompt_tokens: int, completion_tokens: int, rate: Rate | None) -> Money | None:
    """Integer arithmetic on a per-million rate, rounded down.

    Down rather than nearest, deliberately: an estimate that rounded up would
    creep above the real figure across many runs, and an understated estimate
    is easier to argue with than an overstated one.
    """
    if rate is None:
        return None
    minor = (
        prompt_tokens * rate.prompt_minor_per_million
        + completion_tokens * rate.completion_minor_per_million
    ) // TOKENS_PER_MILLION
    return Money(minor, rate.currency)
