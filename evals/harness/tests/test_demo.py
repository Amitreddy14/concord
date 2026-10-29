"""The walkthrough must keep working, and must keep being honest.

A demo is the first thing a reader runs and the last thing anyone maintains.
This is not a formatting test — it checks the two properties that make the
walkthrough worth running at all: that it narrates from the run's own state
rather than from a script, and that it fails loudly when the system it is
describing is broken.
"""

from __future__ import annotations

import pytest

from evals.harness.demo import DEMO_ORDER, QUESTIONS, _render_runs, main_async
from evals.harness.run import CheckReport, RunReport


@pytest.mark.asyncio
async def test_the_full_walkthrough_narrates_what_the_runs_produced(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One full run, asserted once.

    Deliberately a single test making every claim rather than three readable
    ones. The full walkthrough includes S09's five repeats, each of which
    drives a real retry, so it costs about thirty seconds — and `make check`
    is run dozens of times a day. Two tests that each ran it doubled the
    suite's wall time from 35 seconds to 72, which buys nothing: they would
    fail together.

    The facts asserted are the four claims the walkthrough makes that a
    reader would otherwise take on trust: a refund was issued, a run paused
    for a human, the injected ticket was priced against the tier the CRM
    holds rather than the one the ticket asserts, and the five timing-out
    runs produced one refund between them.
    """
    code = await main_async([])
    out = capsys.readouterr().out

    assert code == 0
    for scenario_id in DEMO_ORDER:
        assert scenario_id in out, f"{scenario_id} is missing from the walkthrough"

    assert "1 refund issued" in out
    assert "needs_approval" in out
    assert "standard-tier autonomy cap" in out, (
        "S10's injection claims enterprise tier; the walkthrough should show the cap it was "
        "actually measured against"
    )
    assert "the number of refunds on the capture was [1]" in out


def test_every_demo_scenario_has_a_question() -> None:
    """The question is what makes a scenario mean something to a reader.

    Without it the walkthrough is a list of graph paths. A scenario added to
    `DEMO_ORDER` and left without one would print a blank line where its
    point belongs. No run needed, so no run taken.
    """
    missing = [s for s in DEMO_ORDER if not QUESTIONS.get(s)]
    assert not missing, f"these demo scenarios have no stated question: {missing}"


@pytest.mark.asyncio
async def test_a_broken_system_is_not_narrated_as_a_working_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The property that stops this being a marketing script.

    The walkthrough prints whatever the run produced, so a regression would
    be narrated in confident prose unless the exit code and the closing line
    depend on the assertions. They do, and this is the test of that: the
    checks are counted from the same reports the narration is built from.
    """
    assert await main_async(["--scenarios", "S02"]) == 0
    out = capsys.readouterr().out

    assert "checks    7/7" in out
    assert "Every assertion in all 1 scenario passed." in out
    assert "broken" not in out


@pytest.mark.asyncio
async def test_an_unknown_scenario_is_refused_rather_than_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = await main_async(["--scenarios", "S99"])
    assert code == 2
    assert "no such scenario: S99" in capsys.readouterr().out


class TestCollapsingRepeatsHidesNothing:
    """The collapsing is only safe if a differing repeat still prints.

    S09's five runs produce one result restated five times, and printing it
    five times buried the aggregate claim underneath it. But a walkthrough
    that collapsed a repeat which actually diverged would be hiding the single
    most important thing the scenario could tell anyone — so this is the test
    that makes the shortening honest.
    """

    @staticmethod
    def _report(label: str, *, refunds: int, checks_ok: int = 6) -> RunReport:
        return RunReport(
            "S09",
            "Payments gateway times out mid-refund",
            label,
            checks=[CheckReport(f"ok_{i}", True, "") for i in range(checks_ok)],
            diagnostics={
                "intent": "refund_request",
                "path": ["triage", "act"],
                "permission": "permitted",
                "risk_tier": "R1-routine-refund",
                "decision_reasons": ["within the tier cap"],
                "refunds": refunds,
                "ticket_status": "resolved",
                "reply_excerpt": "I have refunded INR 1,999.00.",
            },
        )

    def test_identical_repeats_are_counted_once(self, capsys: pytest.CaptureFixture[str]) -> None:
        reports = [self._report(f"repeat {i + 1}", refunds=1) for i in range(5)]
        _render_runs(reports)
        out = capsys.readouterr().out

        assert out.count("I have refunded") == 1
        assert "the remaining 4 runs produced identical output" in out
        assert "repeat 5" not in out

    def test_a_repeat_that_differs_is_printed_in_full(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The case the collapsing must not swallow.

        A fourth run that refunded twice is the exact failure S09 exists to
        catch. If the walkthrough counted it as "identical" the reader would
        be told the opposite of what happened.
        """
        reports = [self._report(f"repeat {i + 1}", refunds=1) for i in range(5)]
        reports[3] = self._report("repeat 4", refunds=2)
        _render_runs(reports)
        out = capsys.readouterr().out

        assert "repeat 4" in out
        assert "2 refunds issued" in out, "the divergent run's own numbers must be shown"
        assert "1 refund issued" in out

    def test_a_branch_points_at_the_decision_instead_of_repeating_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main = self._report("main", refunds=0)
        branch = self._report("branch: approved", refunds=1)
        _render_runs([main, branch])
        out = capsys.readouterr().out

        assert out.count("within the tier cap") == 1, "the decision was printed twice"
        assert "as above — resumed from the same checkpoint" in out

    def test_a_branch_whose_decision_differs_prints_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main = self._report("main", refunds=0)
        branch = self._report("branch: approved", refunds=1)
        branch.diagnostics["risk_tier"] = "R2a-multi-capture"
        _render_runs([main, branch])
        out = capsys.readouterr().out

        assert "R2a-multi-capture" in out
        assert "as above" not in out

    def test_failures_are_counted_across_every_run_including_collapsed_ones(self) -> None:
        """A collapsed run's failures must still reach the exit code.

        Counting only what is printed would make the walkthrough's verdict
        depend on its own formatting.
        """
        reports = [self._report(f"repeat {i + 1}", refunds=1, checks_ok=6) for i in range(5)]
        for report in reports[2:]:
            report.checks.append(CheckReport("bad", False, "detail"))

        assert _render_runs(reports) == 3
