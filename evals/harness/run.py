"""The eval runner.

One scenario at a time, each against freshly built services, so a fault
script's per-endpoint attempt counter starts where the fixture expects and no
scenario inherits another's state. Slower than sharing a stack, and the
alternative is a suite whose results depend on ordering.

Three things the fixtures ask for that a naive loop would miss.

**Repeats.** S09 asks for five runs at the same seed with
`require_identical_outcome`. One run producing one refund proves nothing about
whether the idempotency key is derived or merely happened not to collide, so a
repeated scenario must pass every time *and* produce the same outcome.

**Branches.** S04 has four: approved, rejected, modified and expired. Each
resumes the same paused run differently, so each needs its own run from the
pause onward, and the branch's assertions are evaluated on what that resume
produced.

**The restart.** S04 also asks that a paused run survive the process
restarting. The runner rebuilds the graph against the same checkpointer before
resuming and records whether the accumulated facts came back, because a check
that silently passed without the restart happening would be worse than none.

Run as ``python -m evals.harness.run`` (``make eval``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from apps.mocks.faults import FaultConfig, FaultProfile
from apps.mocks.serve import build
from evals.fixtures import Assertion, Scenario, load_all, reset_payload
from evals.harness.checks import CHECKS, Result
from evals.harness.gateway import HarnessGateway
from evals.harness.outcome import Outcome
from packages.agent.context import NodeContext
from packages.agent.deadletter import InMemoryDeadLetterQueue
from packages.agent.graph import build_graph, initial_state
from packages.policy.approval import ApprovalStatus, InMemoryApprovalStore
from packages.policy.tenant import TenantConfig, load_tenant
from packages.shared.ledger import InMemoryLedger
from packages.tools.invoker import ToolInvoker
from packages.tools.tests.conftest import BASE_URLS, RoutingTransport

#: How the runner obtains a gateway for a scenario. The suite's default is
#: the prompt-aware stand-in; the benchmark substitutes a real model. Threaded
#: as a parameter rather than read from the environment, so the two cannot
#: disagree about which is in use — a benchmark that silently measured the
#: stand-in would report perfect scores for every model.
GatewayFactory = Callable[[Scenario], Any]


def default_gateway(scenario: Scenario) -> Any:
    return HarnessGateway(scenario.scenario_id)


class NoSleep:
    """Collapse backoff. The suite measures behaviour, not patience."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@dataclass
class CheckReport:
    name: str
    ok: bool
    detail: str
    expected: Any = None

    def to_payload(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class RunReport:
    scenario_id: str
    title: str
    label: str
    """Which run this was: `main`, `repeat 3`, or a branch name."""
    checks: list[CheckReport] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)
    """The few facts that explain a cluster of failures at a glance.

    Four assertions failing in one scenario is usually one cause, and the
    cause is almost always upstream of all four: a misclassified intent
    sends the run down a path where none of them can pass. Without the
    intent and the path in the report, finding that out means re-running on
    a GPU — so a benchmark that cannot be diagnosed from its own output is
    a benchmark that gets re-run constantly.
    """

    @property
    def ok(self) -> bool:
        return self.error is None and all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[CheckReport]:
        return [c for c in self.checks if not c.ok]

    def to_payload(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario_id,
            "label": self.label,
            "ok": self.ok,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "diagnostics": self.diagnostics,
            "checks": [c.to_payload() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Running one scenario
# ---------------------------------------------------------------------------


@dataclass
class Rig:
    """One scenario's world: its services, its stores, its graph."""

    scenario: Scenario
    tenant: TenantConfig
    services: dict[str, Any]
    approvals: InMemoryApprovalStore
    dead_letters: InMemoryDeadLetterQueue
    ledger: InMemoryLedger
    saver: MemorySaver
    context: NodeContext
    ticket_id: str
    graph: Any

    def restart(self) -> None:
        """Rebuild the graph against the same checkpointer.

        As close to a process restart as a harness gets without forking: a
        new graph object, the same durable state, and a resume that has to
        come from the checkpoint because this object has never seen the run.
        """
        self.graph = build_graph(self.context, self.saver)

    def outcome(self, run_id: str, state: dict[str, Any], *, paused: bool, **extra: Any) -> Outcome:
        payments = self.services["payments"].state.store
        ticketing = self.services["ticketing"].state.store
        crm = self.services["crm"].state.store
        return Outcome(
            scenario=self.scenario,
            run_id=run_id,
            ticket_id=self.ticket_id,
            state=state,
            ledger=self.ledger,
            approvals=self.approvals,
            dead_letters=self.dead_letters,
            refunds=list(payments.refunds.values()),
            tickets=dict(ticketing.tickets),
            messages=dict(ticketing.messages),
            notes=dict(crm.notes),
            paused=paused,
            # Counted from the usage ledger, which the gateway protocol
            # declares, rather than from a `calls` list only the test
            # doubles happen to have. The protocol's own docstring says why
            # `usage` is on it — "leaving it off meant that read was
            # unchecked and would have broken silently on a third
            # implementation" — and reaching past it with a
            # `type: ignore[attr-defined]` is how the benchmark crashed on
            # the first real model it was pointed at. The suppressed warning
            # was the warning.
            model_calls=sum(1 for c in self.context.gateway.usage.calls if c["run_id"] == run_id),
            **extra,
        )


def _open_ticket(services: dict[str, Any], scenario: Scenario) -> str:
    """Open the ticket the run will work, the way intake does.

    With the injector quiet. A scenario's faults describe what happens to the
    run; a hostile profile that also rate-limited intake would fail the
    scenario during setup for a reason it is not about.
    """
    ticketing = services["ticketing"]
    armed = ticketing.state.injector.profile
    ticketing.state.injector.profile = FaultProfile.OFF
    try:
        ticket = scenario.ticket
        with TestClient(ticketing) as client:
            response = client.post(
                "/tickets",
                json={
                    "merchant_id": ticket["merchant_id"],
                    "channel": ticket.get("channel", "chat"),
                    "subject": ticket["subject"],
                    "body": ticket["body"],
                },
                headers={"Idempotency-Key": f"intake:{scenario.scenario_id}"},
            )
            if response.status_code != 201:
                raise RuntimeError(f"intake failed: {response.status_code} {response.text}")
            return str(response.json()["ticket_id"])
    finally:
        ticketing.state.injector.profile = armed


async def _build_rig(
    scenario: Scenario,
    client: httpx.AsyncClient,
    services: dict[str, Any],
    gateway_factory: GatewayFactory = default_gateway,
) -> Rig:
    load_tenant.cache_clear()
    tenant = load_tenant(scenario.tenant)
    ledger = InMemoryLedger()
    approvals = InMemoryApprovalStore()
    context = NodeContext(
        gateway=gateway_factory(scenario),
        invoker=ToolInvoker(
            tenant=tenant,
            base_urls=dict(BASE_URLS),
            client=client,
            ledger=ledger,
            sleep=NoSleep(),
        ),
        tenant_id=scenario.tenant,
        require_citations=tenant.requires_citations,
        min_passage_score=tenant.grounding.get("min_passage_score"),
        approvals=approvals,
    )
    saver = MemorySaver()
    return Rig(
        scenario=scenario,
        tenant=tenant,
        services=services,
        approvals=approvals,
        dead_letters=InMemoryDeadLetterQueue(),
        ledger=ledger,
        saver=saver,
        context=context,
        ticket_id=_open_ticket(services, scenario),
        graph=build_graph(context, saver),
    )


def _seed(scenario: Scenario) -> dict[str, Any]:
    services = {
        name: build(name, FaultConfig(profile=scenario.fault_profile, seed=scenario.fault_seed))
        for name in BASE_URLS
    }
    payload = reset_payload(scenario)
    for app in services.values():
        with TestClient(app) as client:
            response = client.post("/_test/reset", json=payload)
            if response.status_code != 200:
                raise RuntimeError(f"seeding failed: {response.text}")
    return services


def evaluate(outcome: Outcome, assertions: list[Assertion]) -> list[CheckReport]:
    reports: list[CheckReport] = []
    for assertion in assertions:
        checker = CHECKS.get(assertion.name)
        if checker is None:
            # Caught by the totality test in CI; reported rather than
            # skipped so a local run cannot pass by omission.
            reports.append(
                CheckReport(assertion.name, False, "no implementation for this assertion")
            )
            continue
        try:
            result: Result = checker(outcome, assertion)
        except Exception as error:  # a check must not take the suite down
            reports.append(
                CheckReport(
                    assertion.name, False, f"the check raised {type(error).__name__}: {error}"
                )
            )
            continue
        reports.append(CheckReport(assertion.name, result.ok, result.detail, assertion.value))
    return reports


async def run_scenario(
    scenario: Scenario, gateway_factory: GatewayFactory = default_gateway
) -> list[RunReport]:
    """Every run a scenario asks for: the main one, its repeats, its branches."""
    reports: list[RunReport] = []
    repeats = scenario.repeat_runs
    signatures: list[str] = []

    for index in range(repeats):
        label = "main" if repeats == 1 else f"repeat {index + 1}"
        report, signature, rig, run_id, state, paused = await _one_run(
            scenario, label, gateway_factory
        )
        reports.append(report)
        signatures.append(signature)

        # Branches run from the pause of the first run only; repeating four
        # branches five times would measure nothing new.
        if index == 0 and paused and scenario.branches:
            reports.extend(await _branches(scenario, rig, run_id, state))

    if repeats > 1 and scenario.requires_identical_outcome and len(set(signatures)) > 1:
        reports.append(
            RunReport(
                scenario.scenario_id,
                scenario.title,
                "identical outcome",
                checks=[
                    CheckReport(
                        "require_identical_outcome",
                        False,
                        f"{len(set(signatures))} distinct outcomes across {repeats} runs: "
                        f"{sorted(set(signatures))}",
                    )
                ],
            )
        )
    return reports


async def _one_run(
    scenario: Scenario, label: str, gateway_factory: GatewayFactory = default_gateway
) -> tuple[RunReport, str, Rig, str, dict[str, Any], bool]:
    services = _seed(scenario)
    hosts = {
        httpx.URL(u).host: a for u, a in zip(BASE_URLS.values(), services.values(), strict=False)
    }
    started = time.monotonic()

    async with httpx.AsyncClient(transport=RoutingTransport(hosts)) as client:
        rig = await _build_rig(scenario, client, services, gateway_factory)
        run_id = f"run_{scenario.scenario_id.lower()}_{label.replace(' ', '')}"
        config = {"configurable": {"thread_id": f"ticket:{rig.ticket_id}"}}
        ticket = scenario.ticket
        error: str | None = None
        state: dict[str, Any] = {}
        paused = False
        extra: dict[str, Any] = {}

        try:
            async for _ in rig.graph.astream(
                initial_state(
                    run_id=run_id,
                    ticket_id=rig.ticket_id,
                    tenant_id=scenario.tenant,
                    merchant_id=str(ticket["merchant_id"]),
                    subject=str(ticket["subject"]),
                    body=str(ticket["body"]),
                ),
                config=config,
            ):
                pass
            snapshot = await rig.graph.aget_state(config)
            state = dict(snapshot.values)
            paused = bool(snapshot.next)

            if paused and _wants_restart(scenario):
                before = list(state.get("visited") or [])
                rig.restart()
                after = await rig.graph.aget_state(config)
                state = dict(after.values)
                extra = {}
                state["_restarted"] = True
                state["_survived_restart"] = list(state.get("visited") or []) == before
        except Exception as failure:
            error = f"{type(failure).__name__}: {failure}"

        duration = int((time.monotonic() - started) * 1000)
        outcome = rig.outcome(run_id, state, paused=paused, duration_ms=duration, **extra)
        report = RunReport(
            scenario.scenario_id,
            scenario.title,
            label,
            checks=evaluate(outcome, scenario.must) if error is None else [],
            error=error,
            duration_ms=duration,
            diagnostics=diagnose(outcome),
        )
        return report, _signature(outcome), rig, run_id, state, paused


def diagnose(outcome: Outcome) -> dict[str, Any]:
    """The handful of facts that usually explain a whole cluster of failures.

    Everything here is already in state or in the stores; none of it is a
    new measurement. What it buys is being able to read a benchmark result
    without a GPU: `intent` and `path` together say which way the run went,
    and a run that went the wrong way fails every assertion about where it
    should have ended up.
    """
    reply = outcome.reply
    draft = outcome.state.get("draft") or {}
    return {
        "intent": (outcome.state.get("triage") or {}).get("intent"),
        "path": list(outcome.state.get("visited") or ()),
        "permission": outcome.decision.get("permission"),
        "risk_tier": outcome.decision.get("risk_tier"),
        "ticket_status": outcome.ticket.get("status"),
        "ticket_assignee": outcome.ticket.get("assignee"),
        "reply_is_refusal": bool(reply.get("is_refusal")),
        "reply_citations": list(reply.get("citations") or []),
        # The reply itself, truncated. Three diagnoses in a row have stalled
        # here: the citations said the draft was refused and nothing said what
        # the model had written, so distinguishing "stated the fact in another
        # format" from "never stated the fact" needed another GPU run. An
        # excerpt is enough to tell those apart and short enough to live in
        # every report.
        "reply_excerpt": outcome.reply_text[:600],
        # The draft, separately, because `reply` is not the model's output
        # once grounding refuses it — `verify_grounding` replaces it with a
        # refusal whose citations are empty. Reporting only the reply made
        # every refused run read as "the model cited nothing", which is the
        # opposite of the `invalid_citation` case, where it cited something
        # that did not exist. One field made two failure modes identical.
        "draft_citations": list(draft.get("citations") or []),
        "draft_excerpt": str(draft.get("reply") or "")[:600],
        "draft_unsupported_claims": list(draft.get("unsupported_claims") or []),
        # Already names the offending id or the ungrounded claim count.
        "grounding_problems": list((outcome.state.get("grounding") or {}).get("problems") or []),
        "grounding": (outcome.state.get("grounding") or {}).get("verdict"),
        "outbound_messages": len(outcome.outbound),
        "scopes_used": sorted(outcome.scopes_used),
        "refunds": len(outcome.refunds),
    }


def _wants_restart(scenario: Scenario) -> bool:
    return any(a.name == "survives_process_restart_while_pending" for a in scenario.must)


def _signature(outcome: Outcome) -> str:
    """What `require_identical_outcome` compares.

    The refunds and the ticket's resulting status: the facts a merchant and an
    auditor would notice. Deliberately not the reply text, which a sampled
    model is entitled to word differently without the outcome differing.
    """
    refunds = sorted(
        json.dumps({k: r.get(k) for k in ("capture_id", "amount", "reason_code")}, sort_keys=True)
        for r in outcome.refunds
    )
    return json.dumps(
        {"refunds": refunds, "ticket_status": outcome.ticket.get("status")}, sort_keys=True
    )


async def _branches(
    scenario: Scenario, rig: Rig, run_id: str, state: dict[str, Any]
) -> list[RunReport]:
    """Resume the paused run once per branch, each from its own fresh pause."""
    reports: list[RunReport] = []
    for name, branch in scenario.branches.items():
        assertions = [Assertion.parse(raw) for raw in branch.get("must") or []]
        if not assertions:
            continue
        # Each branch needs its own pause: resuming the same thread four ways
        # would have the second resume arrive at an already-decided approval.
        report = await _one_branch(scenario, name, branch, assertions)
        reports.append(report)
    return reports


async def _one_branch(
    scenario: Scenario, name: str, branch: dict[str, Any], assertions: list[Assertion]
) -> RunReport:
    services = _seed(scenario)
    hosts = {
        httpx.URL(u).host: a for u, a in zip(BASE_URLS.values(), services.values(), strict=False)
    }
    started = time.monotonic()

    async with httpx.AsyncClient(transport=RoutingTransport(hosts)) as client:
        rig = await _build_rig(scenario, client, services)
        run_id = f"run_{scenario.scenario_id.lower()}_{name}"
        config = {"configurable": {"thread_id": f"ticket:{rig.ticket_id}"}}
        ticket = scenario.ticket
        error: str | None = None
        state: dict[str, Any] = {}

        try:
            async for _ in rig.graph.astream(
                initial_state(
                    run_id=run_id,
                    ticket_id=rig.ticket_id,
                    tenant_id=scenario.tenant,
                    merchant_id=str(ticket["merchant_id"]),
                    subject=str(ticket["subject"]),
                    body=str(ticket["body"]),
                ),
                config=config,
            ):
                pass

            pending = rig.approvals.pending()
            if not pending:
                raise RuntimeError("the run did not pause, so there is no branch to take")
            approval_id = pending[0].approval_id
            _decide(rig, approval_id, name, branch)

            async for _ in rig.graph.astream(
                Command(resume={"approval_id": approval_id}), config=config
            ):
                pass
            snapshot = await rig.graph.aget_state(config)
            state = dict(snapshot.values)
        except Exception as failure:
            error = f"{type(failure).__name__}: {failure}"

        duration = int((time.monotonic() - started) * 1000)
        outcome = rig.outcome(run_id, state, paused=False, duration_ms=duration, branch=name)
        return RunReport(
            scenario.scenario_id,
            scenario.title,
            f"branch: {name}",
            checks=evaluate(outcome, assertions) if error is None else [],
            error=error,
            duration_ms=duration,
            diagnostics=diagnose(outcome),
        )


def _decide(rig: Rig, approval_id: str, name: str, branch: dict[str, Any]) -> None:
    """Apply the branch's reviewer decision, or let time expire it."""
    if name == "expired":
        # Age the deadline rather than the clock: the sweep reads the
        # deadline, and moving it is the honest way to reach this branch.
        rig.approvals.reschedule(approval_id, datetime.now(UTC) - timedelta(hours=1))
        rig.approvals.sweep_expired()
        return

    if name == "approved":
        rig.approvals.decide(approval_id, status=ApprovalStatus.APPROVED, reviewer="harness")
        return

    if name == "rejected":
        rig.approvals.decide(
            approval_id,
            status=ApprovalStatus.REJECTED,
            reviewer="harness",
            note="rejected by the eval harness",
        )
        return

    if name == "modified":
        row = rig.approvals.get(approval_id)
        if row is None:
            raise RuntimeError(f"approval {approval_id} vanished")
        amount = branch.get("reviewer_amount")
        if amount is None:
            raise RuntimeError("the modified branch states no reviewer_amount")
        rig.approvals.decide(
            approval_id,
            status=ApprovalStatus.MODIFIED,
            reviewer="harness",
            note="reduced by the eval harness",
            modified_proposal={**row.proposal, "amount": amount},
        )
        return

    raise RuntimeError(f"the harness does not know how to take the {name!r} branch")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


async def run_scenario_with_model(
    scenario: Scenario,
    *,
    model: str,
    base_url: str,
    usage: Any = None,
) -> list[RunReport]:
    """One scenario against a real model rather than the stand-in.

    The only substitution is the gateway. Services, fixtures and assertions
    are the ones `make eval` uses, so a difference between two models in the
    benchmark table is a difference in the models.

    The usage ledger is shared across scenarios when one is passed, because
    the benchmark's token columns are per model rather than per scenario.
    """
    from packages.shared.llm import OllamaGateway, UsageLedger  # noqa: PLC0415

    shared_usage = usage if usage is not None else UsageLedger()
    clients: list[httpx.AsyncClient] = []

    def factory(_scenario: Scenario) -> Any:
        client = httpx.AsyncClient(timeout=300)
        clients.append(client)
        return OllamaGateway(
            base_url=base_url,
            model=model,
            client=client,
            api_key=os.environ.get("LLM_API_KEY", "not-needed-for-local"),
            usage=shared_usage,
        )

    try:
        return await run_scenario(scenario, factory)
    finally:
        for client in clients:
            await client.aclose()


def render(reports: list[RunReport]) -> str:
    lines: list[str] = []
    width = max((len(f"{r.scenario_id} {r.label}") for r in reports), default=20)

    for report in reports:
        mark = "pass" if report.ok else "FAIL"
        head = f"{report.scenario_id} {report.label}".ljust(width)
        lines.append(f"  {mark}  {head}  {len(report.checks)} check(s)  {report.duration_ms}ms")
        if report.error:
            lines.append(f"          error: {report.error}")
        for failure in report.failures:
            lines.append(f"          {failure.name}: {failure.detail}")

    passed = sum(1 for r in reports if r.ok)
    lines.append("")
    lines.append(f"  {passed}/{len(reports)} runs passed")
    return "\n".join(lines)


def write_report(reports: list[RunReport], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "eval.json"
    path.write_text(
        json.dumps(
            {
                "runs": [r.to_payload() for r in reports],
                "passed": sum(1 for r in reports if r.ok),
                "total": len(reports),
            },
            indent=2,
        )
    )
    return path


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the golden scenario suite.")
    parser.add_argument("--suite", default="evals/golden", help="unused; kept for the Makefile")
    parser.add_argument("--report", default="evals/reports", type=Path)
    parser.add_argument("--scenario", default=None, help="run one scenario, e.g. S04")
    args = parser.parse_args(argv)

    scenarios = load_all()
    chosen = (
        {args.scenario: scenarios[args.scenario]}
        if args.scenario
        else dict(sorted(scenarios.items()))
    )

    reports: list[RunReport] = []
    for scenario in chosen.values():
        reports.extend(await run_scenario(scenario))

    print(render(reports))
    path = write_report(reports, args.report)
    print(f"\n  report: {path}")
    return 0 if all(r.ok for r in reports) else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    sys.exit(main())
