"""Benchmark the local model shortlist on the scenarios that already exist.

`make eval` answers "does the graph behave"; this answers "which model can
drive it". Same fixtures, same assertions, same services — the only thing
swapped is the gateway, from the prompt-aware stand-in to a real Ollama model.
That sameness is the point: a benchmark on its own prompt set measures the
prompt set, and the question here is which model passes the scenarios this
system actually has.

Four numbers per model, chosen because they are the four that decide the
question on an 8GB card (ADR 0003):

**Pass rate** over hard assertions. First, and alone sufficient to disqualify:
a faster model that skips an approval gate is not a cheaper option, it is a
different product.

**Latency**, as the median and the slowest run. The median says what a
merchant usually waits; the slowest says what the complaint will be about, and
a mean hides both.

**Tokens**, prompt and completion. On a fixed context window a model that
needs more prompt to behave is a model that will fail the long tickets.

**VRAM headroom** with the model resident, read after a warm-up rather than
before, because an idle reading says nothing about what the context length has
to fit into — the mistake `make doctor` made until it was fixed.

Nothing here runs in CI. It needs a GPU and several minutes per model, and a
merge gate that depended on hardware would be a merge gate that breaks when
the hardware is busy. `make eval` is the gate; this is the decision record.

Run as ``python -m evals.harness.benchmark`` (``make benchmark``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from evals.fixtures import Scenario, load_all
from evals.harness import run as harness
from packages.shared.llm import OllamaGateway, UsageLedger

#: The shortlist. Models that fit on an 8GB card with room for an 8k context,
#: and that Ollama reports as supporting tool calling. A model that cannot be
#: pulled is reported as unavailable rather than silently skipped, because a
#: benchmark table with a quietly missing row invites the wrong conclusion.
DEFAULT_SHORTLIST = ("qwen3:8b", "llama3.1:8b", "mistral-nemo:12b", "granite3.1-dense:8b")

#: Scenarios the benchmark runs. All twelve would take long enough that nobody
#: would run it; these six span the behaviours a model can actually get wrong —
#: classification, extraction, grounding, refusal, injection resistance and
#: reporting an action it took.
DEFAULT_SCENARIOS = ("S01", "S03", "S05", "S07", "S10", "S12")


@dataclass
class ModelResult:
    model: str
    available: bool = True
    note: str = ""
    runs: list[harness.RunReport] = field(default_factory=list)
    latencies_ms: list[int] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    vram_used_mib: int | None = None
    vram_total_mib: int | None = None
    declared_checks: int = 0
    """Assertions the chosen scenarios declare, set before the runs happen.

    The denominator has to be fixed in advance, and the first version was
    not: it counted the checks that *ran*, and a scenario that errored
    contributed none. So a model whose run crashed outright scored 21/27
    while one whose run merely failed an assertion scored 26/33 — and the
    crash looked like the better result. The table's whole purpose is
    comparison, and that made it compare different things.
    """

    @property
    def checks(self) -> int:
        # Declared, not observed. An errored scenario still owes its
        # assertions.
        return self.declared_checks or sum(len(r.checks) for r in self.runs)

    @property
    def passed_checks(self) -> int:
        return sum(1 for r in self.runs for c in r.checks if c.ok)

    @property
    def errored(self) -> list[str]:
        """Scenarios that did not finish. Counted as failing every assertion."""
        return sorted({f"{r.scenario_id} ({r.error})" for r in self.runs if r.error})

    @property
    def pass_rate(self) -> float:  # allow-float: a ratio, not an amount
        return self.passed_checks / self.checks if self.checks else 0.0

    @property
    def median_ms(self) -> int:
        return int(statistics.median(self.latencies_ms)) if self.latencies_ms else 0

    @property
    def slowest_ms(self) -> int:
        return max(self.latencies_ms) if self.latencies_ms else 0

    @property
    def headroom_mib(self) -> int | None:
        if self.vram_used_mib is None or self.vram_total_mib is None:
            return None
        return self.vram_total_mib - self.vram_used_mib

    @property
    def failures(self) -> list[str]:
        return sorted({f"{r.scenario_id}:{c.name}" for r in self.runs for c in r.failures})

    def to_payload(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "available": self.available,
            "note": self.note,
            "checks": self.checks,
            "passed": self.passed_checks,
            "pass_rate": round(self.pass_rate, 4),  # allow-float: a ratio
            "median_ms": self.median_ms,
            "slowest_ms": self.slowest_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "vram_used_mib": self.vram_used_mib,
            "vram_total_mib": self.vram_total_mib,
            "headroom_mib": self.headroom_mib,
            "failures": self.failures,
            "errored": self.errored,
            # The runs themselves, with every check's detail and the
            # diagnostics that explain a cluster. Omitting these made the
            # report unreadable without a GPU: the failure names were there
            # and the reasons were not, so the only way to find out why was
            # to run it again.
            "runs": [r.to_payload() for r in self.runs],
        }


def gpu_memory() -> tuple[int, int] | None:
    """Used and total VRAM in MiB, or None when there is no nvidia-smi."""
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        output = (
            subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            .stdout.strip()
            .splitlines()[0]
        )
    except (subprocess.SubprocessError, OSError, IndexError):
        return None
    used, total = (int(part.strip()) for part in output.split(","))
    return used, total


async def pull_and_warm(model: str, base_url: str) -> tuple[bool, str]:
    """Make the model resident, so the VRAM reading means something.

    Returns whether it is usable. A model Ollama cannot serve is reported
    rather than skipped: a table with a quietly absent row reads as a model
    that lost, not one that was never tried.
    """
    async with httpx.AsyncClient(timeout=600) as client:
        try:
            tags = await client.get(f"{base_url.rstrip('/v1')}/api/tags")
            names = {m["name"] for m in tags.json().get("models", [])}
        except (httpx.HTTPError, KeyError, ValueError) as error:
            return False, f"could not reach Ollama: {error}"

        if model not in names:
            return False, f"not pulled; run `ollama pull {model}` first"

        gateway = OllamaGateway(base_url=base_url, model=model, client=client, api_key="not-needed")
        try:
            health = await gateway.health()
        except Exception as error:
            return False, f"health check failed: {error}"
        if not health.get("supports_tool_calling"):
            # Recorded as a result rather than an error. "Cannot do structured
            # output" is a finding about the model, and the most useful column
            # in the table for anyone choosing one.
            return False, "does not support tool calling"

        # One throwaway completion so the weights are resident before VRAM is
        # read. Reading before this reports idle memory, which is the mistake
        # `make doctor` made.
        await gateway.complete(
            system="Reply with the single word: ready.",
            user="ready?",
            node="triage",
            run_id="benchmark_warmup",
            budget_tokens=64,
            max_tokens=8,
        )
    return True, ""


async def benchmark_model(model: str, scenarios: list[Scenario], base_url: str) -> ModelResult:
    result = ModelResult(model=model)
    usable, note = await pull_and_warm(model, base_url)
    if not usable:
        result.available = False
        result.note = note
        return result

    # One ledger across every scenario, because the token columns are per
    # model. Passing a fresh one per scenario would have reported the last
    # scenario's tokens as the model's.
    usage = UsageLedger()

    for scenario in scenarios:
        started = time.monotonic()
        # The one substitution: the real gateway in place of the stand-in.
        # Everything else — services, fixtures, assertions — is what `make
        # eval` uses, so a difference in the table is a difference in the
        # model and not in the harness.
        reports = await harness.run_scenario_with_model(
            scenario, model=model, base_url=base_url, usage=usage
        )
        result.runs.extend(reports)
        result.latencies_ms.append(int((time.monotonic() - started) * 1000))

    result.prompt_tokens = sum(int(c["prompt_tokens"]) for c in usage.calls)
    result.completion_tokens = sum(int(c["completion_tokens"]) for c in usage.calls)

    # Read after the scenarios have run, not after the warm-up alone: the
    # context grows with the work, and the headroom that matters is the
    # smallest one, not the one right after loading the weights.
    memory = gpu_memory()
    if memory:
        result.vram_used_mib, result.vram_total_mib = memory

    return result


def render(results: list[ModelResult]) -> str:
    header = (
        f"  {'model':<24} {'pass':>9} {'median':>8} {'slowest':>8} "
        f"{'prompt':>8} {'compl':>7} {'headroom':>9}"
    )
    lines = [header, "  " + "-" * (len(header) - 2)]

    for result in sorted(results, key=lambda r: (-r.pass_rate, r.median_ms)):
        if not result.available:
            lines.append(f"  {result.model:<24} {'unavailable':>9}   {result.note}")
            continue
        headroom = f"{result.headroom_mib} MiB" if result.headroom_mib is not None else "n/a"
        lines.append(
            f"  {result.model:<24} {result.passed_checks:>4}/{result.checks:<4} "
            f"{result.median_ms:>7}ms {result.slowest_ms:>7}ms "
            f"{result.prompt_tokens:>8} {result.completion_tokens:>7} {headroom:>9}"
        )

    # Errors first, because a scenario that did not finish is a different
    # and worse finding than one that finished and failed an assertion — and
    # the old table hid it entirely behind a smaller denominator.
    erroring = [r for r in results if r.available and r.errored]
    if erroring:
        lines.append("")
        lines.append("  scenarios that did not finish (every assertion counted failed):")
        for result in erroring:
            for entry in result.errored:
                lines.append(f"    {result.model}: {entry}")

    failing = [r for r in results if r.available and r.failures]
    if failing:
        lines.append("")
        lines.append("  what each model got wrong:")
        for result in failing:
            lines.append(f"    {result.model}: {', '.join(result.failures)}")

        # And why, as far as the run's own shape can say. Four assertions
        # failing in one scenario is usually one cause upstream of all four,
        # and the intent and the path are where that shows. Printed here so
        # the table can be acted on without re-running anything on a GPU.
        lines.append("")
        lines.append("  how the failing runs went:")
        for result in failing:
            for run in result.runs:
                if not run.failures and not run.error:
                    continue
                d = run.diagnostics or {}
                lines.append(
                    f"    {result.model} {run.scenario_id}: "
                    f"intent={d.get('intent')} "
                    f"grounding={d.get('grounding')} "
                    f"refusal={d.get('reply_is_refusal')} "
                    f"outbound={d.get('outbound_messages')} "
                    f"ticket={d.get('ticket_status')}/{d.get('ticket_assignee')}"
                )
                lines.append(f"      path: {' -> '.join(d.get('path') or [])}")
                # What it cited and what it wrote. A grounding verdict of
                # `invalid_citation` is unactionable without the id the model
                # invented, and `partial` is unactionable without the prose.
                lines.append(f"      cited: {d.get('reply_citations') or 'nothing'}")
                excerpt = (d.get("reply_excerpt") or "").replace("\n", " ").strip()
                if excerpt:
                    lines.append(f"      wrote: {excerpt[:200]}")

    lines.append("")
    lines.append(
        "  Pass rate first. A faster model that skips an approval gate is not a cheaper option."
    )
    return "\n".join(lines)


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark the local model shortlist.")
    parser.add_argument("--models", default=",".join(DEFAULT_SHORTLIST))
    parser.add_argument("--scenarios", default=",".join(DEFAULT_SCENARIOS))
    parser.add_argument("--report", default=Path("evals/reports"), type=Path)
    parser.add_argument(
        "--base-url", default=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
    )
    args = parser.parse_args(argv)

    catalogue = load_all()
    chosen = [catalogue[s.strip()] for s in args.scenarios.split(",") if s.strip()]
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    print(f"  benchmarking {len(models)} model(s) over {len(chosen)} scenario(s)\n")
    results: list[ModelResult] = []
    for model in models:
        print(f"  … {model}", flush=True)
        results.append(await benchmark_model(model, chosen, args.base_url))

    print()
    print(render(results))

    args.report.mkdir(parents=True, exist_ok=True)
    path = args.report / "benchmark.json"
    path.write_text(
        json.dumps(
            {
                "scenarios": [s.scenario_id for s in chosen],
                "models": [r.to_payload() for r in results],
            },
            indent=2,
        )
    )
    print(f"\n  report: {path}")
    return 0 if any(r.available for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    sys.exit(main())
