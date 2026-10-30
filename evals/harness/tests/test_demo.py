"""The walkthrough must keep working, and must keep being honest.

A demo is the first thing a reader runs and the last thing anyone maintains.
This is not a formatting test — it checks the two properties that make the
walkthrough worth running at all: that it narrates from the run's own state
rather than from a script, and that it fails loudly when the system it is
describing is broken.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pytest

from evals.harness.demo import (
    DEMO_ORDER,
    QUESTIONS,
    _as_markdown,
    _render_runs,
    main_async,
)
from evals.harness.run import CheckReport, RunReport

TRANSCRIPT = Path(__file__).resolve().parents[3] / "docs" / "demo-transcript.md"


#: One slot, written once. A dict rather than a module global because
#: `packages/shared/telemetry` already set that precedent for the same reason
#: — a rebound global is invisible at the call site.
_CACHE: dict[str, str] = {}


async def _walkthrough() -> str:
    """The full walkthrough, run once per test session.

    It drives S09's five retries and costs about twenty seconds, and
    `make check` runs dozens of times a day. Two tests that each ran it took
    the suite from 35 seconds to 72 for no additional coverage, so the output
    is captured once and shared. Cached in a module global rather than a
    pytest fixture because it has to be awaited, and a cache is clearer here
    than an async session fixture.
    """
    if "output" not in _CACHE:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = await main_async([])
        assert code == 0, f"the walkthrough exited {code}:\n{buffer.getvalue()[-2000:]}"
        _CACHE["output"] = buffer.getvalue()
    return _CACHE["output"]


@pytest.mark.asyncio
async def test_the_narration_is_read_from_the_runs() -> None:
    """The four claims a reader would otherwise take on trust.

    A refund was issued, a run paused for a human, the injected ticket was
    priced against the tier the CRM holds rather than the one the ticket
    asserts, and the five timing-out runs produced one refund between them.
    """
    out = await _walkthrough()

    for scenario_id in DEMO_ORDER:
        assert scenario_id in out, f"{scenario_id} is missing from the walkthrough"

    assert "1 refund issued" in out
    assert "needs_approval" in out
    assert "standard-tier autonomy cap" in out, (
        "S10's injection claims enterprise tier; the walkthrough should show the cap it was "
        "actually measured against"
    )
    assert "the number of refunds on the capture was [1]" in out


@pytest.mark.asyncio
async def test_the_committed_transcript_is_what_the_command_prints() -> None:
    """The transcript is verified, not trusted.

    A reviewer reads `docs/demo-transcript.md` instead of installing Python,
    Ollama and Docker, so it is the most-read artefact in the repository and
    the one with no natural pressure to stay correct. Committing output and
    hoping is how a README comes to describe a system that no longer exists.

    The demo is byte-deterministic — the stand-in is seeded, approval ids are
    content hashes, and no timings are printed — so this can be an equality
    check rather than a smoke test.
    """
    committed = TRANSCRIPT.read_text(encoding="utf-8")
    expected = _as_markdown(await _walkthrough())

    assert committed == expected, (
        "docs/demo-transcript.md is not what `make demo` prints. Run "
        "`make demo-transcript` and commit the result."
    )


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


class TestTheReadmeQuotesRealOutput:
    """The README's excerpt must be output, not a description of output.

    It is the most-read text in the repository and the least likely to be
    re-checked: a reviewer who reads only the first screen gets their whole
    impression from it. An excerpt that drifted — or that was tidied up to
    read better — would be the one piece of this project making a claim
    nothing verifies, in the place where a reader is least able to tell.

    The first version of the excerpt was already wrong. It dropped the
    `intent` and `path` lines while presenting as verbatim, which is an
    abridgement nobody marked.
    """

    README = Path(__file__).resolve().parents[3] / "README.md"

    @staticmethod
    def _fenced_blocks(text: str) -> list[str]:
        blocks: list[str] = []
        parts = text.split("```text\n")
        for part in parts[1:]:
            blocks.append(part.split("```", 1)[0])
        return blocks

    def test_every_quoted_block_appears_in_the_transcript(self) -> None:
        transcript = TRANSCRIPT.read_text(encoding="utf-8")
        blocks = self._fenced_blocks(self.README.read_text(encoding="utf-8"))

        assert blocks, "the README quotes no walkthrough output; the hook is gone"
        for block in blocks:
            body = block.rstrip("\n")
            assert body in transcript, (
                "a ```text block in the README is not a contiguous slice of "
                "docs/demo-transcript.md:\n"
                f"{body[:400]}"
            )

    def test_the_quoted_block_carries_the_point(self) -> None:
        """Quoting real output is not enough if it is the wrong output.

        The excerpt earns its place by showing the claim and its refutation
        together: a ticket asserting enterprise tier, and a decision priced
        against the standard-tier cap.
        """
        quoted = "\n".join(self._fenced_blocks(self.README.read_text(encoding="utf-8")))

        assert "verified enterprise tier" in quoted
        assert "standard-tier autonomy cap" in quoted
