"""The ADR 0001 invariant, checked structurally rather than observed.

    A node that interrupts performs no mutation before the interrupt.

``test_approval_gate.py`` asserts the *consequence* — the ledger is empty while
a run is paused — and that assertion is the one that would catch the bug today.
This module asserts the *shape*, which is what catches it tomorrow: the day
someone adds a second interrupting node, or moves a tool call into
``await_approval`` to save a round trip, the behavioural test only fails if a
scenario happens to cover the new path. This one fails because the function
body is wrong.

The check reads the source. That is unusual for a unit test and right here,
because the property is about code structure and LangGraph's re-execution
semantics, not about any particular run: everything above an ``interrupt()``
call runs at least twice, so a mutation above one is a mutation performed
twice.
"""

from __future__ import annotations

import ast
from pathlib import Path

AGENT_PACKAGE = Path(__file__).resolve().parent.parent

#: How a node mutates a system of record: the tool invoker. Matched on the
#: attribute name rather than the full path so `context.invoker.call`,
#: `self.invoker.call` and a renamed local all count.
MUTATION_CALL = "call"
INVOKER_ATTRIBUTE = "invoker"
INTERRUPT_NAMES = {"interrupt"}


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _calls_interrupt(node: ast.AST) -> bool:
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id in INTERRUPT_NAMES
        for call in ast.walk(node)
    )


def _invoker_calls(node: ast.AST) -> list[ast.Call]:
    """Every `<something>.invoker.call(...)` inside this node."""
    found: list[ast.Call] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr != MUTATION_CALL:
            continue
        owner = call.func.value
        if isinstance(owner, ast.Attribute) and owner.attr == INVOKER_ATTRIBUTE:
            found.append(call)
    return found


def _sources() -> list[tuple[Path, ast.Module]]:
    return [(path, ast.parse(path.read_text())) for path in sorted(AGENT_PACKAGE.glob("*.py"))]


class TestTheInvariant:
    def test_no_function_both_interrupts_and_calls_a_tool(self) -> None:
        offenders: list[str] = []
        for path, tree in _sources():
            for function in _functions(tree):
                if not _calls_interrupt(function):
                    continue
                calls = _invoker_calls(function)
                if calls:
                    lines = ", ".join(str(c.lineno) for c in calls)
                    offenders.append(
                        f"{path.name}:{function.lineno} {function.name} interrupts and "
                        f"calls the tool invoker at line(s) {lines}"
                    )

        assert not offenders, (
            "ADR 0001: a node that interrupts performs no mutation before the "
            "interrupt, because LangGraph re-executes an interrupted node from "
            "its first line. Split the mutation into its own node.\n  " + "\n  ".join(offenders)
        )

    def test_the_check_would_actually_catch_a_violation(self) -> None:
        # A structural test that cannot fail is worse than no test, because it
        # reads as coverage. This proves the detector fires on the shape it
        # claims to detect.
        bad = ast.parse(
            "async def await_approval(state, context):\n"
            "    await context.invoker.call('create_refund', {}, run_id='r', node='n')\n"
            "    return interrupt({'kind': 'approval_required'})\n"
        )
        function = _functions(bad)[0]
        assert _calls_interrupt(function)
        assert len(_invoker_calls(function)) == 1

    def test_exactly_one_node_interrupts(self) -> None:
        # Not a style preference. Each interrupting node is a place the
        # re-execution rule has to hold, and the invariant is cheap to keep
        # while there is one such node and easy to break once there are
        # several. If this fails, the new node needs its own review — and this
        # number needs updating deliberately, not reflexively.
        interrupting = [
            f"{path.name}:{function.name}"
            for path, tree in _sources()
            for function in _functions(tree)
            if _calls_interrupt(function)
        ]
        assert interrupting == ["gate.py:await_approval"], interrupting


class TestTheMutatingNode:
    def test_the_node_that_refunds_does_not_interrupt(self) -> None:
        tree = ast.parse((AGENT_PACKAGE / "gate.py").read_text())
        act = next(f for f in _functions(tree) if f.name == "act")

        assert _invoker_calls(act), "act is the node that moves money; it should call the tool"
        assert not _calls_interrupt(act), (
            "act must never interrupt: a node that both mutates and interrupts mutates twice"
        )
