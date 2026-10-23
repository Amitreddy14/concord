"""Everything an assertion is allowed to look at.

Assembled once per scenario run and passed to every check, so no check does
its own I/O and every check sees the same world. A check that fetched its own
facts could pass against a service it had just changed.

The fields are the systems of record plus the run's own artefacts. There is
deliberately no model and no gateway handle: a hard assertion that could ask a
model is a hard assertion that is no longer deterministic, and the split
between `must` and `should` is the whole reason this harness is trustworthy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from evals.fixtures import Scenario
from packages.agent.deadletter import DeadLetterQueue
from packages.policy.approval import ApprovalStore
from packages.shared.ledger import InMemoryLedger, RunStep, StepKind


@dataclass(frozen=True, slots=True)
class Outcome:
    scenario: Scenario
    run_id: str
    ticket_id: str
    state: dict[str, Any]
    ledger: InMemoryLedger
    approvals: ApprovalStore
    dead_letters: DeadLetterQueue

    #: The stores, read straight from the mock services after the run.
    refunds: list[dict[str, Any]] = field(default_factory=list)
    tickets: dict[str, dict[str, Any]] = field(default_factory=dict)
    messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: dict[str, dict[str, Any]] = field(default_factory=dict)

    paused: bool = False
    """Whether the graph is still waiting at an interrupt."""
    model_calls: int = 0
    duration_ms: int = 0
    branch: str | None = None
    """Which of a scenario's decision branches this run took, if any."""

    # -- derived views the checks share ---------------------------------

    @property
    def steps(self) -> list[RunStep]:
        return self.ledger.steps(self.run_id)

    @property
    def tools_called(self) -> set[str]:
        return {s.tool for s in self.steps if s.tool}

    @property
    def scopes_used(self) -> set[str]:
        return {s.scope for s in self.steps if s.scope}

    @property
    def ticket(self) -> dict[str, Any]:
        """The ticket this run worked, or an empty mapping if it never existed."""
        return self.tickets.get(self.ticket_id, {})

    @property
    def reply(self) -> dict[str, Any]:
        return self.state.get("reply") or {}

    @property
    def reply_text(self) -> str:
        return str(self.reply.get("text") or "")

    @property
    def decision(self) -> dict[str, Any]:
        return self.state.get("decision") or {}

    @property
    def outbound(self) -> list[dict[str, Any]]:
        """Messages actually sent to the merchant.

        Read from the ticketing store rather than from state, because what
        the merchant received is what was posted — a reply the agent drafted
        and failed to post is not a reply.
        """
        return [
            m
            for m in self.messages.values()
            if m.get("ticket_id") == self.ticket_id and m.get("direction") == "outbound"
        ]

    def steps_of(self, kind: StepKind) -> list[RunStep]:
        return [s for s in self.steps if s.kind is kind]

    def payload_of(self, step: RunStep, which: str) -> Any:
        return self.ledger.payload(self.run_id, step.seq, which)

    def tool_inputs(self, tool: str) -> list[dict[str, Any]]:
        """Every set of arguments this run sent to a tool."""
        found: list[dict[str, Any]] = []
        for step in self.steps:
            if step.tool != tool:
                continue
            payload = self.payload_of(step, "input")
            if isinstance(payload, dict):
                found.append(payload)
        return found
