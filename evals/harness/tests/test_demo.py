"""The walkthrough must keep working, and must keep being honest.

A demo is the first thing a reader runs and the last thing anyone maintains.
This is not a formatting test — it checks the two properties that make the
walkthrough worth running at all: that it narrates from the run's own state
rather than from a script, and that it fails loudly when the system it is
describing is broken.
"""

from __future__ import annotations

import pytest

from evals.harness.demo import DEMO_ORDER, QUESTIONS, main_async


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

    assert "refund(s) issued" in out
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
