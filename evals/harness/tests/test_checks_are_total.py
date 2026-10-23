"""Every assertion a fixture names has an implementation, and vice versa.

This is the test that makes the harness worth trusting. Without it a typo in
a fixture becomes an assertion nobody checks, and the suite reports a pass it
never verified — which is worse than a failing suite, because a failing suite
gets looked at.

The reverse direction matters too: a check registered for a name no fixture
uses is dead code that reads as coverage, and the day someone relies on it
they will find it was never exercised.
"""

from __future__ import annotations

import pytest

from evals.fixtures import Assertion, Scenario, load_all
from evals.harness.checks import CHECKS, Result


def asserted_names() -> set[str]:
    """Every `must` name across every fixture, including branch assertions."""
    names: set[str] = set()
    for scenario in load_all().values():
        names |= {a.name for a in scenario.must}
        for branch in scenario.branches.values():
            for raw in branch.get("must") or []:
                names.add(Assertion.parse(raw).name)
    return names


class TestTotality:
    def test_the_fixtures_name_something(self) -> None:
        # Reflection that found nothing would make every assertion below pass
        # vacuously, which is how the dependency checker once passed while
        # scanning no files at all.
        names = asserted_names()
        assert len(names) >= 40, f"only {len(names)} assertion names found across the fixtures"

    def test_every_asserted_name_is_implemented(self) -> None:
        missing = sorted(asserted_names() - set(CHECKS))
        assert not missing, (
            "fixtures assert names with no implementation, so the suite would report a "
            f"pass it never verified: {missing}"
        )

    def test_every_implementation_is_asserted_somewhere(self) -> None:
        unused = sorted(set(CHECKS) - asserted_names())
        assert not unused, (
            "checks registered for names no fixture uses; dead code that reads as "
            f"coverage: {unused}"
        )

    @pytest.mark.parametrize("name", sorted(CHECKS), ids=lambda n: n)
    def test_each_check_returns_a_result(self, name: str) -> None:
        # Not a behaviour test — the scenario runs cover that. This catches a
        # check that raises on an empty outcome, which would turn one
        # scenario's failure into the whole suite erroring out.
        from evals.harness.outcome import Outcome
        from packages.agent.deadletter import InMemoryDeadLetterQueue
        from packages.policy.approval import InMemoryApprovalStore
        from packages.shared.ledger import InMemoryLedger

        empty = Outcome(
            scenario=next(iter(load_all().values())),
            run_id="run_empty",
            ticket_id="tkt_empty",
            state={},
            ledger=InMemoryLedger(),
            approvals=InMemoryApprovalStore(),
            dead_letters=InMemoryDeadLetterQueue(),
        )
        result = CHECKS[name](empty, Assertion(name=name, value=0))
        assert isinstance(result, Result)
        assert isinstance(result.detail, str)


class TestScenarioCoverage:
    @pytest.mark.parametrize("scenario_id", sorted(load_all()), ids=lambda s: s)
    def test_every_scenario_has_hard_assertions(self, scenario_id: str) -> None:
        # A scenario with only graded checks cannot fail CI, which makes it a
        # description rather than a test.
        scenario: Scenario = load_all()[scenario_id]
        assert scenario.must, f"{scenario_id} has no must assertions"


class TestPinnedScopes:
    def test_every_tool_has_a_pinned_scope(self) -> None:
        """A new tool must be added to the pinned table deliberately.

        Otherwise `no_tool_scope_widened` silently stops covering it, which
        is the failure mode that check exists to prevent.
        """
        from evals.harness.checks import EXPECTED_SCOPES
        from packages.tools import registry

        missing = sorted(set(registry.TOOLS) - set(EXPECTED_SCOPES))
        assert not missing, f"tools with no pinned scope: {missing}"

    def test_the_pinned_table_has_no_phantom_tools(self) -> None:
        from evals.harness.checks import EXPECTED_SCOPES
        from packages.tools import registry

        phantom = sorted(set(EXPECTED_SCOPES) - set(registry.TOOLS))
        assert not phantom, f"pinned scopes for tools that do not exist: {phantom}"

    def test_the_pinned_table_currently_matches_the_registry(self) -> None:
        """They agree today. When they stop, one of them is a regression.

        This test says which: if the registry changed deliberately, update
        the pinned table in the same commit and a reviewer sees both halves.
        """
        from evals.harness.checks import EXPECTED_SCOPES
        from packages.tools import registry

        drifted = {
            name: (spec.scope, EXPECTED_SCOPES[name])
            for name, spec in registry.TOOLS.items()
            if name in EXPECTED_SCOPES and spec.scope != EXPECTED_SCOPES[name]
        }
        assert not drifted, f"registry and pinned scopes disagree: {drifted}"
