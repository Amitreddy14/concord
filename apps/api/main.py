"""The intake and run-control API.

Asynchronous by construction. ``POST /tickets`` enqueues a run and returns its
id; progress is streamed over SSE or polled. On local inference a resolution
takes tens of seconds, so a synchronous endpoint would mean a client holding a
connection open through the whole thing (ADR 0003).

Phase 6 adds the approval endpoints the reviewer console calls. Phase 5 moves
the run store and the checkpointer to Postgres.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

import httpx
from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from apps.api import approvals as approval_logic
from apps.api.runs import Run, RunStatus, RunStore
from packages.agent.checkpointing import checkpointer as checkpointer_context
from packages.agent.checkpointing import thread_id_for
from packages.agent.graph import build_graph, initial_state
from packages.agent.nodes import NodeContext
from packages.agent.structured import StructuredOutputError
from packages.policy.approval import (
    ApprovalStore,
    InMemoryApprovalStore,
)
from packages.policy.tenant import TenantConfig, load_tenant
from packages.shared.ledger import InMemoryLedger
from packages.shared.llm import LLMError, LLMGateway, OllamaGateway
from packages.shared.problem import PROBLEM_CONTENT_TYPE, VALIDATION_FAILED, ProblemError
from packages.tools.errors import ScopeViolation, ToolError
from packages.tools.invoker import ToolInvoker


class SubmitTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_id: str = Field(pattern=r"^M-[0-9]{4}$")
    channel: str = Field(default="chat")
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    ticket_id: str | None = None


@dataclass
class Dependencies:
    """Everything a run needs, assembled once at startup."""

    tenant: TenantConfig
    gateway: LLMGateway
    base_urls: dict[str, str]
    client: httpx.AsyncClient
    ledger: InMemoryLedger
    store: RunStore
    approvals: ApprovalStore = field(default_factory=InMemoryApprovalStore)
    """Shared by the graph and the API. One store, because the node that
    pauses and the endpoint that answers are looking at the same queue — two
    stores would make an approval recorded by the API invisible to the run
    waiting for it."""
    database_url: str | None = None
    """When absent the checkpointer is in-memory, which is a development
    convenience only: a deployment running without durable checkpoints loses
    every pending approval on restart and looks fine until it does."""

    _stack: AsyncExitStack | None = field(default=None, repr=False)
    _saver: Any | None = field(default=None, repr=False)

    async def checkpointer(self) -> Any:
        """The one checkpointer this process uses, opened on first use.

        One, and not one per run. Opening a checkpointer inside each graph
        drive was a bug with two faces: against Postgres it built a connection
        pool per ticket, and against the in-memory saver it handed every
        resume a brand-new empty store — so a paused run could not be found by
        the request resuming it, and the API reported a KeyError from a node
        reading state that was never loaded. Scoping it to the process is what
        makes a pause and its resume look at the same thing.
        """
        if self._saver is None:
            self._stack = AsyncExitStack()
            self._saver = await self._stack.enter_async_context(
                checkpointer_context(self.database_url)
            )
        return self._saver

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._saver = None


def service_urls() -> dict[str, str]:
    return {
        "crm": os.environ.get("CRM_BASE_URL", "http://localhost:8101"),
        "payments": os.environ.get("PAYMENTS_BASE_URL", "http://localhost:8102"),
        "ticketing": os.environ.get("TICKETING_BASE_URL", "http://localhost:8103"),
        "knowledge": os.environ.get("KNOWLEDGE_BASE_URL", "http://localhost:8104"),
    }


def get_dependencies(request: Request) -> Dependencies:
    deps: Dependencies = request.app.state.deps
    return deps


def _context_for(deps: Dependencies) -> NodeContext:
    invoker = ToolInvoker(
        tenant=deps.tenant,
        base_urls=deps.base_urls,
        client=deps.client,
        ledger=deps.ledger,
    )
    return NodeContext(
        gateway=deps.gateway,
        invoker=invoker,
        tenant_id=deps.tenant.tenant_id,
        require_citations=deps.tenant.requires_citations,
        min_passage_score=deps.tenant.grounding.get("min_passage_score"),
        approvals=deps.approvals,
    )


async def execute(run: Run, deps: Dependencies, submission: SubmitTicket) -> None:
    """Start a run from a submitted ticket."""
    run.status = RunStatus.RUNNING
    run.emit("run.started", ticket_id=run.ticket_id)
    state = initial_state(
        run_id=run.run_id,
        ticket_id=run.ticket_id,
        tenant_id=deps.tenant.tenant_id,
        merchant_id=submission.merchant_id,
        subject=submission.subject,
        body=submission.body,
    )
    await _drive(run, deps, state)


async def resume_run(run: Run, deps: Dependencies, *, approval_id: str) -> None:
    """Continue a run that was waiting for a human.

    The resume payload names the approval, and that is all it does. The
    authority is the stored record, which ``await_approval`` re-reads — an
    approval id arriving over HTTP is the one input here an attacker could
    shape, and it must not be the thing that authorises a refund.
    """
    run.status = RunStatus.RUNNING
    run.emit("run.resumed", approval_id=approval_id)
    await _drive(run, deps, Command(resume={"approval_id": approval_id}))


async def _drive(run: Run, deps: Dependencies, graph_input: Any) -> None:
    """Run the graph to its next stopping point.

    Every failure becomes a terminal run state rather than an exception that
    disappears into a background task. A run that vanishes silently is worse
    than one that fails loudly, because nobody knows to look.

    "Stopping point" and not "completion": the graph may stop because it is
    waiting for a human, which is a healthy outcome and must not be recorded
    as a finished run.
    """
    context = _context_for(deps)

    # One thread per ticket rather than per run: a ticket resumed after an
    # approval is the same conversation, and must see what the first run
    # established rather than starting from nothing.
    config = {"configurable": {"thread_id": thread_id_for(run.ticket_id)}}

    try:
        saver = await deps.checkpointer()
        graph = build_graph(context, saver)
        async for update in graph.astream(graph_input, config=config):
            for node, payload in update.items():
                run.emit("node.completed", node=node, keys=sorted(payload or {}))
        snapshot = await graph.aget_state(config=config)
        final = dict(snapshot.values)
        paused = bool(snapshot.next)
        interrupts = [i.value for task in snapshot.tasks for i in task.interrupts]
    except (ToolError, ScopeViolation, LLMError, StructuredOutputError) as error:
        run.failure = f"{type(error).__name__}: {error}"
        run.finish(RunStatus.FAILED, reason=run.failure)
        return
    except Exception as error:
        run.failure = f"unexpected {type(error).__name__}: {error}"
        run.finish(RunStatus.FAILED, reason=run.failure)
        return

    if paused:
        # Waiting for a reviewer. The run is not finished and must not be
        # marked so: a completed run stops emitting, and the console would
        # show a resolved ticket with a refund nobody authorised pending
        # behind it.
        run.status = RunStatus.AWAITING_APPROVAL
        pending = next((i for i in interrupts if isinstance(i, dict)), {})
        run.emit(
            "run.awaiting_approval",
            approval_id=pending.get("approval_id"),
            expires_at=pending.get("expires_at"),
        )
        return

    run.result = {
        "intent": (final.get("triage") or {}).get("intent"),
        "resolution": final.get("resolution"),
        "reply": final.get("reply"),
        "grounding": final.get("grounding"),
        "draft": final.get("draft"),
        "decision": final.get("decision"),
        "approval": final.get("approval"),
        "refund": final.get("refund"),
        "passages": [p.get("passage_id") for p in final.get("passages") or []],
        "visited": final.get("visited", []),
        "usage": deps.gateway.usage.for_run(run.run_id),
    }

    if final.get("failure") == "approval_expired":
        # Abandoned rather than failed: nothing went wrong technically, a
        # human simply never answered. S04's expired branch asks for exactly
        # this distinction.
        run.finish(RunStatus.ABANDONED, reason="approval expired without a decision")
        return
    if final.get("status") == "failed":
        run.finish(RunStatus.FAILED, reason=final.get("failure") or "run failed")
        return
    run.finish(RunStatus.COMPLETED)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    client = httpx.AsyncClient()
    gateway = OllamaGateway(
        base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
        model=os.environ.get("LLM_MODEL_PRIMARY", "qwen3:8b"),
        client=client,
        api_key=os.environ.get("LLM_API_KEY", "not-needed-for-local"),
    )
    app.state.deps = Dependencies(
        tenant=load_tenant(os.environ.get("CONCORD_TENANT", "acme-payments")),
        gateway=gateway,
        base_urls=service_urls(),
        client=client,
        ledger=InMemoryLedger(),
        store=RunStore(),
        database_url=os.environ.get("DATABASE_URL"),
    )
    sweeper = asyncio.create_task(_sweep_forever(app.state.deps))
    try:
        yield
    finally:
        sweeper.cancel()
        await app.state.deps.aclose()
        await client.aclose()


def create_app(deps: Dependencies | None = None) -> FastAPI:
    app = FastAPI(title="Concord intake API", version="0.1.0", lifespan=lifespan)
    if deps is not None:
        # Injected for tests: skip the lifespan's real gateway and services.
        app.router.lifespan_context = _noop_lifespan
        app.state.deps = deps

    @app.exception_handler(ProblemError)
    async def _problem(_request: Request, exc: ProblemError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.problem.status,
            content=exc.problem.to_payload(),
            media_type=PROBLEM_CONTENT_TYPE,
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(deps: Annotated[Dependencies, Depends(get_dependencies)]) -> dict[str, Any]:
        """Is the agent actually able to work?

        Separate from /health on purpose: the process can be up while the
        model is absent, and a deploy that reports healthy in that state is
        reporting the wrong thing.
        """
        try:
            gateway = await deps.gateway.health()
        except LLMError as error:
            return {"ready": False, "reason": str(error)}
        return {
            "ready": bool(gateway.get("supports_tool_calling")),
            "model": gateway.get("model"),
            "tenant": deps.tenant.tenant_id,
        }

    @app.post("/tickets", status_code=202)
    async def submit(
        deps: Annotated[Dependencies, Depends(get_dependencies)],
        submission: Annotated[SubmitTicket, Body()],
    ) -> dict[str, Any]:
        run = deps.store.create(
            ticket_id=submission.ticket_id or f"tkt_{submission.merchant_id.lower()}",
            tenant_id=deps.tenant.tenant_id,
            merchant_id=submission.merchant_id,
        )
        # Held on the app so the task is not garbage collected mid-flight,
        # which is a real and very confusing failure mode.
        task = asyncio.create_task(execute(run, deps, submission))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
        return {"run_id": run.run_id, "ticket_id": run.ticket_id, "status": str(run.status)}

    @app.get("/runs/{run_id}")
    async def get_run(
        run_id: str, deps: Annotated[Dependencies, Depends(get_dependencies)]
    ) -> dict[str, Any]:
        run = deps.store.get(run_id)
        if run is None:
            raise ProblemError(VALIDATION_FAILED, f"no run with id {run_id}")
        return run.to_payload()

    @app.get("/runs/{run_id}/steps")
    async def get_steps(
        run_id: str, deps: Annotated[Dependencies, Depends(get_dependencies)]
    ) -> dict[str, Any]:
        """The ledger for one run — what the reviewer console reads."""
        if deps.store.get(run_id) is None:
            raise ProblemError(VALIDATION_FAILED, f"no run with id {run_id}")
        return {
            "run_id": run_id,
            "steps": [
                {
                    "seq": s.seq,
                    "node": s.node,
                    "kind": str(s.kind),
                    "outcome": str(s.outcome),
                    "tool": s.tool,
                    "scope": s.scope,
                    # The rule a policy step turned on. Present so the run
                    # trace can show which rule decided, rather than only
                    # that a decision happened.
                    "policy_rule_id": s.policy_rule_id,
                    "attempt": s.attempt,
                    "duration_ms": s.duration_ms,
                    "error_code": s.error_code,
                }
                for s in deps.ledger.steps(run_id)
            ],
            "scopes_used": sorted(deps.ledger.scopes_used(run_id)),
        }

    @app.get("/runs/{run_id}/stream")
    async def stream(
        run_id: str, deps: Annotated[Dependencies, Depends(get_dependencies)]
    ) -> StreamingResponse:
        run = deps.store.get(run_id)
        if run is None:
            raise ProblemError(VALIDATION_FAILED, f"no run with id {run_id}")

        async def events() -> AsyncIterator[str]:
            queue = run.subscribe()
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        yield "event: done\ndata: {}\n\n"
                        return
                    yield f"data: {json.dumps(event.to_payload())}\n\n"
            finally:
                run.unsubscribe(queue)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # The approval endpoints live in their own router: they are the console's
    # entire surface, and they have their own validation rules.
    app.include_router(
        approval_logic.build_router(
            get_deps=get_dependencies,
            resume=resume_run,
            sweep=_sweep,
            background=_BACKGROUND,
        )
    )
    return app


_BACKGROUND: set[asyncio.Task[None]] = set()

#: How often the expiry sweep runs. Minutes rather than seconds because an
#: approval window is measured in hours, and rather than hours because a
#: merchant waiting on an expired request should not wait an extra one.
SWEEP_INTERVAL_SECONDS = 300


async def _sweep(deps: Dependencies) -> dict[str, Any]:
    """Expire overdue approvals and resume each waiting run so it terminates.

    Expiring the row is only half of it. The run is still paused inside the
    graph, and a row marked expired beside a thread that never moves is the
    same silence as no sweep at all — so each one is resumed, which takes the
    rejected-or-expired branch out of `await_approval` and ends the run.
    """
    expired = deps.approvals.sweep_expired()
    resumed: list[str] = []
    for request in expired:
        run = deps.store.get(request.run_id)
        if run is None:
            continue
        await resume_run(run, deps, approval_id=request.approval_id)
        resumed.append(request.approval_id)
    return {
        "expired": [r.approval_id for r in expired],
        "resumed": resumed,
    }


async def _sweep_forever(deps: Dependencies) -> None:
    """The loop LangGraph does not provide.

    Errors are swallowed per iteration on purpose: a sweep that dies on one
    bad row stops expiring every other approval, and the failure mode of this
    loop going quiet is a queue that silently stops timing out.
    """
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await _sweep(deps)
        except Exception:
            logging.getLogger(__name__).exception("approval sweep failed")


@asynccontextmanager
async def _noop_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield


app = create_app()
