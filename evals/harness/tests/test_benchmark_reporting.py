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

    def test_the_table_says_what_to_read_first(self) -> None:
        # The sentence is the point of the whole exercise: a model that
        # skips an approval gate is not a cheaper option.
        assert "Pass rate first" in render([result("m", passed=1, failed=0, median=10)])
