"""The LangGraph StateGraph for the vertical slice.

Linear for now — triage, gather, retrieve, draft — with the supervisor and
specialist subgraphs arriving in phase 5. Built as a graph from the start
rather than as a function chain because the properties that matter later are
structural: a durable checkpoint per step, and a pause point that survives a
restart (ADR 0001). Retrofitting those onto a chain is a rewrite.

The checkpointer is in-memory here. Phase 5 swaps in Postgres behind the same
interface, which is the whole reason the interface is used now.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from packages.agent import nodes
from packages.agent.state import AgentState

NodeFn = Callable[[AgentState], Coroutine[Any, Any, dict[str, Any]]]


def build_graph(context: nodes.NodeContext, checkpointer: Any | None = None) -> Any:
    """Compile the slice.

    Nodes are bound to their context here rather than reading a global, so a
    test can supply a scripted gateway and an invoker pointed at in-process
    services without patching anything.
    """
    builder: StateGraph[AgentState] = StateGraph(AgentState)

    def bind(fn: Callable[..., Coroutine[Any, Any, dict[str, Any]]]) -> NodeFn:
        async def wrapped(state: AgentState) -> dict[str, Any]:
            return await fn(state, context)

        wrapped.__name__ = fn.__name__
        return wrapped

    # mypy cannot match a plain async callable over a TypedDict against
    # add_node's overload set, though the call is correct at runtime. Narrowed
    # to this one call rather than relaxing the type check for the module.
    for name, fn in (
        ("triage", nodes.triage),
        ("gather", nodes.gather),
        ("retrieve", nodes.retrieve),
        ("draft", nodes.draft),
        ("verify", nodes.verify_grounding),
    ):
        builder.add_node(name, bind(fn))  # type: ignore[call-overload]

    builder.add_edge(START, "triage")
    builder.add_edge("triage", "gather")
    builder.add_edge("gather", "retrieve")

    # Retrieval returning nothing is an answer, not a failure: the corpus does
    # not cover the question. Drafting anyway would produce a fluent reply
    # with nothing behind it, which is the single most expensive thing this
    # system could do. The edge skips to the verifier, which turns the empty
    # result into a refusal.
    builder.add_conditional_edges(
        "retrieve",
        _has_passages,
        {"draft": "draft", "verify": "verify"},
    )
    builder.add_edge("draft", "verify")
    builder.add_edge("verify", END)

    return builder.compile(checkpointer=checkpointer or MemorySaver())


def _has_passages(state: AgentState) -> str:
    return "draft" if state.get("passages") else "verify"


def initial_state(
    *, run_id: str, ticket_id: str, tenant_id: str, merchant_id: str, subject: str, body: str
) -> AgentState:
    return AgentState(
        run_id=run_id,
        ticket_id=ticket_id,
        tenant_id=tenant_id,
        merchant_id=merchant_id,
        subject=subject,
        body=body,
        status="running",
        failure=None,
        visited=[],
    )
