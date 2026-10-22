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
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.api.runs import Run, RunStatus, RunStore
from packages.agent.graph import build_graph, initial_state
from packages.agent.nodes import NodeContext
from packages.agent.structured import StructuredOutputError
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


async def execute(run: Run, deps: Dependencies, submission: SubmitTicket) -> None:
    """Run the graph, emitting progress as it goes.

    Every failure becomes a terminal run state rather than an exception that
    disappears into a background task. A run that vanishes silently is worse
    than one that fails loudly, because nobody knows to look.
    """
    run.status = RunStatus.RUNNING
    run.emit("run.started", ticket_id=run.ticket_id)

    invoker = ToolInvoker(
        tenant=deps.tenant,
        base_urls=deps.base_urls,
        client=deps.client,
        ledger=deps.ledger,
    )
    context = NodeContext(gateway=deps.gateway, invoker=invoker, tenant_id=deps.tenant.tenant_id)
    graph = build_graph(context)

    state = initial_state(
        run_id=run.run_id,
        ticket_id=run.ticket_id,
        tenant_id=deps.tenant.tenant_id,
        merchant_id=submission.merchant_id,
        subject=submission.subject,
        body=submission.body,
    )

    try:
        async for update in graph.astream(
            state, config={"configurable": {"thread_id": run.ticket_id}}
        ):
            for node, payload in update.items():
                run.emit("node.completed", node=node, keys=sorted(payload or {}))
        snapshot = await graph.aget_state(config={"configurable": {"thread_id": run.ticket_id}})
        final = dict(snapshot.values)
    except (ToolError, ScopeViolation, LLMError, StructuredOutputError) as error:
        run.failure = f"{type(error).__name__}: {error}"
        run.finish(RunStatus.FAILED, reason=run.failure)
        return
    except Exception as error:
        run.failure = f"unexpected {type(error).__name__}: {error}"
        run.finish(RunStatus.FAILED, reason=run.failure)
        return

    run.result = {
        "intent": (final.get("triage") or {}).get("intent"),
        "reply": final.get("reply"),
        "grounding": final.get("grounding"),
        "draft": final.get("draft"),
        "passages": [p.get("passage_id") for p in final.get("passages") or []],
        "visited": final.get("visited", []),
        "usage": deps.gateway.usage.for_run(run.run_id),
    }
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
    )
    try:
        yield
    finally:
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

    return app


_BACKGROUND: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def _noop_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield


app = create_app()
