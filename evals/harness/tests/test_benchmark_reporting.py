"""The benchmark's arithmetic and its table, without needing a GPU.

The run itself needs hardware and several minutes per model, so it is not in
CI. What is in CI is everything around it: the pass-rate arithmetic, the
ordering that puts correctness before speed, and the unavailable path — the
one that decides whether a model Ollama cannot serve appears as a row or
vanishes. A table with a quietly missing row reads as a model that lost rather
than one that was never tried.
"""

from __future__ import annotations

from evals.harness.benchmark import ModelResult, render
from evals.harness.run import CheckReport, RunReport


def result(
    model: str,
    *,
    passed: int,
    failed: int,
    median: int,
    available: bool = True,
    note: str = "",
) -> ModelResult:
    checks = [CheckReport(f"ok_{i}", True, "") for i in range(passed)]
    checks += [CheckReport(f"bad_{i}", False, "detail") for i in range(failed)]
    report = RunReport("S01", "title", "main", checks=checks)
    return ModelResult(
        model=model,
        available=available,
        note=note,
        runs=[report],
        latencies_ms=[median],
        prompt_tokens=1000,
        completion_tokens=200,
        vram_used_mib=6000,
        vram_total_mib=8188,
    )


class TestArithmetic:
    def test_pass_rate_counts_checks_not_runs(self) -> None:
        # A run with nine passing checks and one failure is not a 0% model,
        # and it is not a 100% one either.
        model = result("m", passed=9, failed=1, median=100)
        assert model.checks == 10
        assert model.passed_checks == 9
        assert abs(model.pass_rate - 0.9) < 0.001  # allow-float: a ratio

    def test_an_empty_run_is_zero_rather_than_a_division_error(self) -> None:
        model = ModelResult(model="m")
        assert model.pass_rate == 0

    def test_headroom_is_total_minus_used(self) -> None:
        model = result("m", passed=1, failed=0, median=100)
        assert model.headroom_mib == 2188

    def test_headroom_is_none_without_a_gpu(self) -> None:
        # Reported as absent rather than as zero: zero headroom and no
        # reading are very different findings.
        model = ModelResult(model="m")
        assert model.headroom_mib is None

    def test_the_slowest_run_is_reported_beside_the_median(self) -> None:
        # The median says what a merchant usually waits; the slowest says
        # what the complaint will be about. A mean hides both.
        model = ModelResult(model="m", latencies_ms=[100, 120, 140, 9000])
        assert model.median_ms == 130
        assert model.slowest_ms == 9000


class TestTheTable:
    def test_correctness_orders_before_speed(self) -> None:
        fast_and_wrong = result("fast", passed=5, failed=5, median=100)
        slow_and_right = result("slow", passed=10, failed=0, median=5000)
        table = render([fast_and_wrong, slow_and_right])
        assert table.index("slow") < table.index("fast"), (
            "a faster model that fails assertions must not head the table"
        )

    def test_speed_breaks_a_tie(self) -> None:
        quick = result("quick", passed=10, failed=0, median=100)
        sluggish = result("sluggish", passed=10, failed=0, median=5000)
        table = render([sluggish, quick])
        assert table.index("quick") < table.index("sluggish")

    def test_an_unavailable_model_is_a_row_with_its_reason(self) -> None:
        table = render(
            [
                result("works", passed=4, failed=0, median=100),
                result(
                    "missing",
                    passed=0,
                    failed=0,
                    median=0,
                    available=False,
                    note="does not support tool calling",
                ),
            ]
        )
        assert "missing" in table
        assert "unavailable" in table
        # The reason, which is the most useful column for anyone choosing.
        assert "does not support tool calling" in table

    def test_failures_are_named_so_the_table_can_be_acted_on(self) -> None:
        table = render([result("m", passed=1, failed=2, median=100)])
        assert "what each model got wrong" in table
        assert "S01:bad_0" in table

    def test_a_failing_run_reports_what_the_model_cited_and_wrote(self) -> None:
        """The two fields that make a grounding verdict actionable.

        `grounding=invalid_citation` says the model cited something that does
        not exist, and is unactionable without the id it invented.
        `grounding=partial` says the prose outran the citations, and is
        unactionable without the prose. Both were absent, so three
        consecutive diagnoses needed another GPU run to see what the model
        had actually produced.
        """
        model = result("m", passed=0, failed=1, median=100)
        model.runs[0].diagnostics = {
            "intent": "settlement_query",
            "path": ["triage", "draft"],
            "grounding": "invalid_citation",
            "draft_citations": ["kb:settlements:invented-id"],
            "draft_excerpt": "Your payout is expected on 3 November.",
            "grounding_problems": [
                "cited passages that were not retrieved: kb:settlements:invented-id"
            ],
            "reply_excerpt": "I could not verify the policy, so a colleague will pick this up.",
        }
        table = render([model])

        assert "kb:settlements:invented-id" in table
        assert "3 November" in table
        assert "problem: cited passages that were not retrieved" in table

    def test_a_refused_draft_is_reported_instead_of_the_systems_own_refusal(self) -> None:
        """The model's output and the system's substitute are different things.

        Reporting only the reply made every refused run print `cited:
        nothing`, because `verify_grounding` replaces the draft with a
        refusal that cites nothing. That is indistinguishable from a model
        that genuinely returned no citations — the two bugs we had been
        chasing, collapsed into one line.
        """
        model = result("m", passed=0, failed=1, median=100)
        model.runs[0].diagnostics = {
            "grounding": "invalid_citation",
            "draft_citations": ["kb:made-up"],
            "draft_excerpt": "Your payout clears on Tuesday.",
            "reply_excerpt": "A colleague will pick this up.",
        }
        table = render([model])

        assert "cited: ['kb:made-up']" in table
        assert "drafted: Your payout clears on Tuesday." in table
        assert "sent instead: A colleague will pick this up." in table

    def test_an_accepted_draft_is_not_printed_twice(self) -> None:
        model = result("m", passed=0, failed=1, median=100)
        model.runs[0].diagnostics = {
            "grounding": "supported",
            "draft_excerpt": "Your payout clears on Tuesday.",
            "reply_excerpt": "Your payout clears on Tuesday.",
        }
        table = render([model])

        assert table.count("Your payout clears on Tuesday.") == 1
        assert "sent instead" not in table

    def test_a_failing_run_with_no_reply_omits_the_line_rather_than_printing_empty(
        self,
    ) -> None:
        model = result("m", passed=0, failed=1, median=100)
        model.runs[0].diagnostics = {"grounding": "unsupported", "draft_citations": []}
        table = render([model])

        assert "cited: nothing" in table
        assert "drafted:" not in table

    def test_the_table_says_what_to_read_first(self) -> None:
        # The sentence is the point of the whole exercise: a model that
        # skips an approval gate is not a cheaper option.
        assert "Pass rate first" in render([result("m", passed=1, failed=0, median=10)])


class TestTheBenchmarkPathUsesOnlyTheProtocol:
    """The runner must not reach past `LLMGateway` into a double's internals.

    This is the test that was missing. `Rig.outcome` read
    `gateway.calls` — present on both test doubles, absent from the
    protocol and from `OllamaGateway` — behind a
    `type: ignore[attr-defined]`, so nothing caught it until the benchmark
    was pointed at a real model and crashed on its first scenario. The
    suppressed warning was the warning.

    The gateway here implements the protocol and nothing else, which is the
    only way to assert that the runner needs nothing else.
    """

    async def test_a_protocol_only_gateway_completes_a_scenario(self) -> None:
        import json
        from dataclasses import dataclass, field

        from evals.fixtures import load_all
        from evals.harness import run as harness
        from evals.harness.gateway import TRIAGE, TRIAGE_DEFAULTS
        from packages.shared.llm import Completion, UsageLedger

        @dataclass
        class ProtocolOnlyGateway:
            """Exactly `LLMGateway`: `model`, `usage`, `complete`, `health`.

            Deliberately no `calls` list, no inspection hooks, nothing a test
            double would add for convenience. If the runner needs any of
            that, this fails — which is the point.
            """

            model: str = "protocol-only"
            usage: UsageLedger = field(default_factory=UsageLedger)

            async def complete(
                self,
                *,
                system: str,
                user: str,
                node: str,
                run_id: str,
                budget_tokens: int,
                json_schema: dict[str, object] | None = None,
                temperature: float = 0.0,  # allow-float: the protocol's knob
                max_tokens: int = 1024,
            ) -> Completion:
                if node == "triage":
                    text = json.dumps({**TRIAGE_DEFAULTS, **TRIAGE["S01"]})
                else:
                    text = json.dumps(
                        {
                            "reply": "Card refunds reach the cardholder in 5 to 7 working days.",
                            "citations": _passages(system + user),
                            "unsupported_claims": [],
                        }
                    )
                completion = Completion(
                    text=text,
                    model=self.model,
                    prompt_tokens=10,
                    completion_tokens=10,
                    latency_ms=1,
                )
                self.usage.record(run_id=run_id, node=node, completion=completion)
                return completion

            async def health(self) -> dict[str, object]:
                return {"model": self.model, "supports_tool_calling": True}

        def _passages(prompt: str) -> list[str]:
            import re

            return re.findall(r"^(psg_[\w.\-]+):", prompt, re.MULTILINE)

        reports = await harness.run_scenario(
            load_all()["S01"], lambda _scenario: ProtocolOnlyGateway()
        )

        assert reports, "the scenario produced no report"
        assert all(r.error is None for r in reports), [r.error for r in reports]
        # And the model-call count came from the usage ledger, which is the
        # only place the protocol exposes it.
        failures = [(c.name, c.detail) for r in reports for c in r.failures]
        assert all(r.ok for r in reports), failures


class TestTheDenominatorIsFixedInAdvance:
    """A crash must score worse than a failed assertion, not better.

    The first version counted the checks that ran, so a scenario that
    errored contributed none — and a model whose run crashed scored 21/27
    while one whose run merely failed an assertion scored 26/33. The crash
    looked like the better result, in a table whose entire purpose is
    comparison.
    """

    def test_an_errored_scenario_still_owes_its_assertions(self) -> None:
        crashed = ModelResult(
            model="crashes",
            declared_checks=33,
            runs=[RunReport("S03", "t", "main", checks=[], error="StructuredOutputError")],
        )
        assert crashed.checks == 33
        assert crashed.passed_checks == 0
        assert crashed.pass_rate == 0

    def test_a_crash_ranks_below_a_failed_assertion(self) -> None:
        crashed = ModelResult(
            model="crashes",
            declared_checks=10,
            runs=[RunReport("S03", "t", "main", checks=[], error="boom")],
        )
        failed_one = ModelResult(
            model="fails-one",
            declared_checks=10,
            runs=[
                RunReport(
                    "S03",
                    "t",
                    "main",
                    checks=[CheckReport("a", True, "")] * 9 + [CheckReport("b", False, "d")],
                )
            ],
        )
        table = render([crashed, failed_one])
        assert table.index("fails-one") < table.index("crashes")

    def test_an_error_is_named_rather_than_hidden_in_the_denominator(self) -> None:
        table = render(
            [
                ModelResult(
                    model="m",
                    declared_checks=6,
                    runs=[
                        RunReport("S03", "t", "main", checks=[], error="StructuredOutputError: x")
                    ],
                )
            ]
        )
        assert "did not finish" in table
        assert "S03" in table
        assert "StructuredOutputError" in table
