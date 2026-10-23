"""The suite's own acceptance test: it fails on a regression that matters.

Phase 8 is done when CI fails on an injected regression — "a widened scope, a
skipped gate, a dropped citation". A gate nobody has watched fail is a gate
nobody knows works, and the failure mode is silent: the suite passes, and goes
on passing after the thing it protects has broken.

So each regression here is injected into the real code and the real suite is
run against it. Monkeypatching rather than editing files, so a crashed test
cannot leave the repository broken, and each patch is the smallest change that
produces the defect rather than a deliberately obvious one.

These are slow — each one runs a scenario end to end. They earn it: they are
the only tests in the repository that check the tests.
"""

from __future__ import annotations

import pytest

from evals.fixtures import load_all
from evals.harness import run as harness


async def _run(scenario_id: str) -> list[harness.RunReport]:
    return await harness.run_scenario(load_all()[scenario_id])


def _failed_checks(reports: list[harness.RunReport]) -> set[str]:
    return {c.name for r in reports for c in r.failures}


class TestTheSuitePassesCleanly:
    async def test_s03_passes_before_anything_is_broken(self) -> None:
        # The control. Without it, a regression test that "catches" something
        # may just be catching a scenario that never passed.
        reports = await _run("S03")
        assert all(r.ok for r in reports), _failed_checks(reports)


class TestWidenedScope:
    async def test_a_tool_calling_outside_its_declared_scope_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tool that quietly uses a scope it does not declare.

        The shape an injected instruction aims for: the tool still works, the
        run still completes, and the scope recorded in the ledger is one the
        deployment never granted for that call.
        """
        import dataclasses

        from packages.tools import registry

        # ToolSpec is frozen, which is the right design and means the
        # regression has to replace the registry entry rather than mutate the
        # spec — closer to how a real mistake would arrive anyway: someone
        # edits the registry.
        widened = dataclasses.replace(
            registry.get("search_knowledge"), scope="payments:refund:write"
        )
        monkeypatch.setitem(registry.TOOLS, "search_knowledge", widened)

        # S10 is the scenario that asserts this, because S10 is the
        # injection scenario and a widened scope is what an injected
        # instruction is reaching for.
        reports = await _run("S10")
        failed = _failed_checks(reports)
        assert failed, "the suite passed while a tool used a scope it does not declare"
        assert "no_tool_scope_widened" in failed


class TestSkippedGate:
    async def test_a_refund_above_the_cap_without_approval_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The approval gate, removed.

        Patched at the policy engine so every decision comes back permitted —
        the way a mis-edited tier table or an inverted condition would behave.
        The run then refunds S04's ₹41,200 with no human anywhere near it.
        """
        from packages.agent import gate
        from packages.policy.engine import Decision, Permission

        def always_permitted(action: object, **kwargs: object) -> Decision:
            return Decision(
                permission=Permission.PERMITTED,
                rule_id="R1-routine-refund",
                risk_tier="R1-routine-refund",
                required_scope="payments:refund:write",
                reasons=["injected regression: the gate is gone"],
            )

        monkeypatch.setattr(gate, "evaluate", always_permitted)

        reports = await _run("S04")
        failed = _failed_checks(reports)
        assert failed, "the suite passed while a refund above the cap skipped approval"
        # The main run should have paused and did not, so the pending-state
        # assertions are the ones that go red.
        assert {"approval_record_exists", "risk_tier_is", "ticket_status_is"} & failed


class TestDroppedCitation:
    async def test_a_reply_sent_without_its_citations_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Citations dropped between drafting and posting.

        Not a model that failed to cite — the verifier catches that. This is
        the subtler one: a grounded reply whose citations are lost on the way
        to the ticket, so the merchant receives an assertion with nothing
        behind it and every upstream check still passes.
        """
        from packages.agent import settle as settle_module

        original = settle_module.settle

        async def strip_citations(state: dict[str, object], context: object) -> object:
            reply = dict(state.get("reply") or {})
            if reply:
                reply["citations"] = []
                state = {**state, "reply": reply}
            return await original(state, context)

        monkeypatch.setattr(settle_module, "settle", strip_citations)
        # The graph binds nodes at build time, so the patch has to reach the
        # module the builder reads.
        from packages.agent import graph as graph_module

        monkeypatch.setattr(graph_module.settle_node, "settle", strip_citations)

        reports = await _run("S01")
        failed = _failed_checks(reports)
        assert failed, "the suite passed while a reply reached the merchant uncited"
        assert "citations_non_empty" in failed
