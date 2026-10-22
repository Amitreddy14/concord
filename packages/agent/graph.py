"""The agent graph.

Triage classifies, a supervisor routes to one specialist, retrieval grounds,
the drafter writes, and the verifier decides whether the reply may be sent.

The supervisor is a conditional edge rather than a node that calls a model.
Intent was classified by triage; routing is a lookup. Asking a model to decide
twice adds latency and a second opportunity to be wrong.

Two branches end the run early, and both are successful outcomes rather than
failures. An ambiguous payment reference terminates in a question, because a
silent choice between two payments is the defect S11 exists to catch. An empty
retrieval skips drafting, because a reply written against nothing is the most
expensive thing this system could produce.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from langgraph.graph import END, START, StateGraph

from packages.agent import nodes, specialists
from packages.agent.checkpointing import checkpointer as checkpointer_context
from packages.agent.state import AgentState

NodeFn = Callable[[AgentState], Coroutine[Any, Any, dict[str, Any]]]

__all__ = ["build_graph", "checkpointer_context", "initial_state"]


def build_graph(context: nodes.NodeContext, checkpointer: Any | None = None) -> Any:
    """Compile the graph.

    Nodes are bound to their context here rather than reading a global, so a
    test can supply a scripted gateway and an in-process invoker without
    patching anything.
    """
    builder: StateGraph[AgentState] = StateGraph(AgentState)

    def bind(fn: Callable[..., Coroutine[Any, Any, dict[str, Any]]]) -> NodeFn:
        async def wrapped(state: AgentState) -> dict[str, Any]:
            return await fn(state, context)

        wrapped.__name__ = fn.__name__
        return wrapped

    core = (
        ("triage", nodes.triage),
        ("gather", nodes.gather),
        ("retrieve", nodes.retrieve),
        ("draft", nodes.draft),
        ("verify", nodes.verify_grounding),
    )
    for name, fn in core:
        builder.add_node(name, bind(fn))  # type: ignore[call-overload]
    for name, fn in specialists.SPECIALISTS.items():
        builder.add_node(name, bind(fn))  # type: ignore[call-overload]

    builder.add_edge(START, "triage")
    builder.add_edge("triage", "gather")

    # The supervisor: one hop, from classified intent to the specialist that
    # reads what that intent needs and nothing else.
    builder.add_conditional_edges(
        "gather",
        specialists.route,
        {name: name for name in specialists.SPECIALISTS},
    )

    # A specialist that resolved a question — an ambiguous reference, a
    # payment that does not exist — has finished the run.
    for name in specialists.SPECIALISTS:
        builder.add_conditional_edges(
            name,
            _specialist_next,
            {"retrieve": "retrieve", "end": END},
        )

    builder.add_conditional_edges("retrieve", _has_passages, {"draft": "draft", "verify": "verify"})
    builder.add_edge("draft", "verify")
    builder.add_edge("verify", END)

    return builder.compile(checkpointer=checkpointer)


def _specialist_next(state: AgentState) -> str:
    """Stop if the specialist already produced the run's reply."""
    return "end" if state.get("reply") else "retrieve"


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
