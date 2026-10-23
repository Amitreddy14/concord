"""Replaying a run from what was recorded, and proving it decided the same.

A run that issued a refund last Tuesday is an argument about what the system
did. Without replay, settling that argument means reading a ledger and
reasoning about it; with replay, the run is re-executed against the responses
it actually received and the decisions it reaches are compared to the ones it
reached then.

The substitution is at the tool layer. ``RecordedInvoker`` presents the same
interface as ``ToolInvoker`` and answers from the ledger instead of the
network, matching on the digest of the arguments — so a replayed call gets
back precisely what the original call got back, and a call the original never
made has no recording to serve.

That last part is the design's spine, and it is why a miss raises rather than
falls through to the network. A replay that quietly reached a live service
would be measuring today's systems of record, not reproducing a past run, and
it could move money while doing it. A miss means the replay diverged, and
divergence is the finding, not an inconvenience.

What "the same decisions" means is deliberately narrow: the policy decisions
(rule id, permission, risk tier), the proposal, and whether a mutation
happened. Not the drafted reply — that comes from a sampled model, and
pinning it would make the check fail on a reworded sentence while a changed
rule id slipped past. The decisions are the part with consequences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from packages.shared.ledger import InMemoryLedger, StepKind, digest
from packages.tools import registry
from packages.tools.errors import ErrorKind, ToolError, ToolFailure
from packages.tools.invoker import ToolResult


class ReplayError(Exception):
    """The recording cannot answer what the replay asked for."""


@dataclass
class RecordedInvoker:
    """Serves tool responses from a ledger instead of calling anything.

    Implements the slice of ``ToolInvoker`` the nodes use: ``call``, plus the
    ``ledger`` and ``tenant`` attributes they read. It is not a subclass,
    because inheriting would make it one network-configuration mistake away
    from issuing a real refund during a replay.
    """

    source: InMemoryLedger
    source_run_id: str
    ledger: InMemoryLedger
    tenant: Any = None

    _responses: dict[tuple[str, str], Any] = field(default_factory=dict, init=False)
    _failures: dict[tuple[str, str], str] = field(default_factory=dict, init=False)
    served: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._index()

    def _index(self) -> None:
        """Index the original run's successful calls by tool and argument digest.

        Only the completed steps carry an output. A `started` step is the
        other half of the same call and holds the input, which is what the
        digest is taken from.

        The payload keys are the ledger's own — `input` and `output` — and
        not a second vocabulary invented here. Phase 6 wrote `request` and
        `response` into two of these calls, which read fine and made the
        payloads invisible to anything looking them up by the names the
        ledger documents.
        """
        for step in self.source.steps(self.source_run_id):
            if step.kind is not StepKind.TOOL_CALL or step.tool is None:
                continue
            if step.input_digest is None:
                continue
            key = (step.tool, step.input_digest)
            response = self.source.payload(self.source_run_id, step.seq, "output")
            if response is not None:
                self._responses[key] = response
            elif step.error_code:
                # A call that failed in the original run fails the same way
                # in the replay. Serving a success instead would make the
                # replay a different run that happens to share a ticket.
                self._failures.setdefault(key, step.error_code)

    @staticmethod
    def _canonical(tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        """The payload the recorder digested, not the arguments as passed.

        `ToolInvoker` validates arguments through the tool's Pydantic model
        and records the digest of `to_payload()`. Digesting the raw dict here
        instead produced a different key for the same call the moment a model
        filled in a default — which it did: the replay diverged on
        `search_knowledge` while the recording held exactly the call it was
        looking for. The replay has to canonicalise the way the recorder did,
        so it does it by calling the same model.
        """
        spec = registry.get(tool_name)
        try:
            return dict(spec.args_model.model_validate(arguments or {}).to_payload())
        except ValidationError as error:
            # Arguments the model rejects cannot match any recording, because
            # the original invoker would have refused them as POISONED_INPUT
            # before they reached the network. So this is a divergence too,
            # and it gets the divergence error rather than a raw Pydantic
            # traceback surfacing from inside a replay.
            raise ReplayError(
                f"the replay called {tool_name} with arguments the tool rejects, so it "
                f"cannot match anything the original recorded: {error.errors()[0]['msg']} "
                f"at {'.'.join(str(p) for p in error.errors()[0]['loc'])}"
            ) from error

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        run_id: str,
        node: str,
        correlation_id: str | None = None,
    ) -> ToolResult:
        key = (tool_name, digest(self._canonical(tool_name, arguments)))
        self.served.append(tool_name)

        if key in self._responses:
            return ToolResult(
                tool=tool_name,
                data=self._responses[key],
                attempts=1,
                replayed=True,
            )

        if key in self._failures:
            raise ToolError(
                ToolFailure(
                    kind=ErrorKind.FATAL,
                    code=self._failures[key],
                    status=None,
                    detail="recorded failure, replayed",
                    tool=tool_name,
                )
            )

        # No recording. The replay asked something the original never asked,
        # which means it diverged — and the one thing that must not happen
        # is falling through to a live service.
        raise ReplayError(
            f"the original run made no {tool_name} call with these arguments, so the "
            f"replay has diverged. Recorded {tool_name} calls: "
            f"{sum(1 for t, _ in self._responses if t == tool_name)}."
        )


@dataclass(frozen=True, slots=True)
class Decisions:
    """The part of a run worth comparing across a replay."""

    rule_ids: tuple[str, ...]
    permissions: tuple[str, ...]
    risk_tiers: tuple[str, ...]
    proposal: dict[str, Any] | None
    refund_id: str | None
    visited: tuple[str, ...]

    def differences(self, other: Decisions) -> list[str]:
        """What changed, named one line at a time.

        A boolean would say a replay diverged; an operator needs to know
        where, because "the rule id changed" and "the graph took a different
        path" are different investigations.
        """
        found: list[str] = []
        for label, mine, theirs in (
            ("rule ids", self.rule_ids, other.rule_ids),
            ("permissions", self.permissions, other.permissions),
            ("risk tiers", self.risk_tiers, other.risk_tiers),
            ("path", self.visited, other.visited),
        ):
            if mine != theirs:
                found.append(f"{label}: {list(mine)} then {list(theirs)}")
        if self.proposal != other.proposal:
            found.append(f"proposal: {self.proposal} then {other.proposal}")
        if self.refund_id != other.refund_id:
            found.append(f"refund: {self.refund_id} then {other.refund_id}")
        return found


def decisions_from_ledger(ledger: InMemoryLedger, run_id: str) -> tuple[str, ...]:
    """The rule ids a run recorded, in order."""
    return tuple(
        step.policy_rule_id
        for step in ledger.steps(run_id)
        if step.kind is StepKind.POLICY_DECISION and step.policy_rule_id
    )


def decisions_from_state(state: dict[str, Any], ledger: InMemoryLedger, run_id: str) -> Decisions:
    decision = state.get("decision") or {}
    return Decisions(
        rule_ids=decisions_from_ledger(ledger, run_id),
        permissions=(str(decision.get("permission")),) if decision else (),
        risk_tiers=(str(decision.get("risk_tier")),) if decision else (),
        proposal=state.get("proposal"),
        refund_id=(state.get("refund") or {}).get("refund_id"),
        # The nodes a run visited. A replay that reaches the same decisions
        # by a different path is still worth knowing about.
        visited=tuple(state.get("visited") or ()),
    )
