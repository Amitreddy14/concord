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

from packages.agent import gate, nodes, specialists
from packages.agent import settle as settle_node
from packages.agent.checkpointing import checkpointer as checkpointer_context
from packages.agent.state import AgentState
from packages.policy.approval import PROCEEDING
from packages.policy.engine import Permission
from packages.shared import telemetry

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
            # The span is applied here rather than inside each node, so a new
            # node is traced without its author remembering to — and so the
            # span name is the graph's name for the node rather than whatever
            # the function happens to be called.
            with telemetry.node_span(fn.__name__, run_id=str(state.get("run_id", ""))):
                return await fn(state, context)

        wrapped.__name__ = fn.__name__
        return wrapped

    core = (
        ("triage", nodes.triage),
        ("gather", nodes.gather),
        ("retrieve", nodes.retrieve),
        ("propose", gate.propose),
        ("decide", gate.decide),
        ("await_approval", gate.await_approval),
        ("act", gate.act),
        ("draft", nodes.draft),
        ("verify", nodes.verify_grounding),
        ("settle", settle_node.settle),
        ("mark_pending", settle_node.mark_pending),
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
    # payment that does not exist — has finished the work, but not the run:
    # the question still has to reach the merchant, so it goes to settle
    # rather than to END. A reply that never leaves the graph is a reply the
    # merchant never got.
    for name in specialists.SPECIALISTS:
        builder.add_conditional_edges(
            name,
            _specialist_next,
            {"retrieve": "retrieve", "settle": "settle"},
        )

    # Retrieval grounds the reply. A run that is going to move money goes
    # through the gate first, because a drafted reply that promises a refund
    # the policy engine then refuses is worse than no reply at all.
    builder.add_conditional_edges(
        "retrieve",
        _after_retrieval,
        {"propose": "propose", "draft": "draft", "verify": "verify"},
    )

    builder.add_conditional_edges("propose", _has_proposal, {"decide": "decide", "draft": "draft"})
    # A decision needing a human marks the ticket before the graph pauses.
    # Without that hop the run stops at the gate and the ticket stays `open`,
    # so nothing anywhere says a refund is waiting on someone — which is what
    # S04's `ticket_status_is: awaiting_approval` has always asked for.
    builder.add_conditional_edges(
        "decide",
        _after_decision,
        {"act": "act", "mark_pending": "mark_pending", "draft": "draft"},
    )
    builder.add_edge("mark_pending", "await_approval")

    # The gate's two exits. An approved or modified proposal proceeds to the
    # one node that mutates; a rejection or an expiry goes straight to the
    # drafter, which explains the outcome rather than announcing a refund.
    builder.add_conditional_edges(
        "await_approval", _after_approval, {"act": "act", "draft": "draft"}
    )
    builder.add_edge("act", "draft")

    builder.add_edge("draft", "verify")
    builder.add_edge("verify", "settle")
    builder.add_edge("settle", END)

    return builder.compile(checkpointer=checkpointer)


def _specialist_next(state: AgentState) -> str:
    """Stop if the specialist already produced the run's reply."""
    return "settle" if state.get("reply") else "retrieve"


# Intents that can end in money moving. Everything else skips the refund path
# rather than proposing an action and having it refused: a status question has
# nothing to propose.
# `dispute_notice` is deliberately absent: a chargeback notice proposes
# accepting the chargeback, which is forbidden, rather than a refund.
# `complaint_no_action` too: a complaint about service is not a request
# for money, and proposing a refund for one would be the agent inventing
# a remedy nobody asked for.
REFUND_INTENTS = frozenset({"refund_request"})


def _after_retrieval(state: AgentState) -> str:
    """Route to the gate when there is something to decide.

    Two ways in. A refund intent with captures to refund against, and an
    intent whose answer is an account action the engine has to classify
    (S08). Everything else goes straight to the drafter.
    """
    intent = str((state.get("triage") or {}).get("intent", ""))
    if intent in REFUND_INTENTS and state.get("captures"):
        return "propose"
    if intent in gate.PROPOSAL_FOR_INTENT:
        return "propose"
    return "draft" if state.get("passages") else "verify"


def _has_proposal(state: AgentState) -> str:
    return "decide" if state.get("proposal") else "draft"


def _after_decision(state: AgentState) -> str:
    """Route on the permission, and on nothing else.

    Four permissions, three destinations. `forbidden` and `refused` both go to
    the drafter because both need explaining to a merchant, and neither offers
    an approval — a reviewer cannot authorise a capture outside the refund
    window any more than they can authorise a forbidden action, and putting
    either in an approval queue would imply they could.
    """
    permission = (state.get("decision") or {}).get("permission")
    if permission == str(Permission.PERMITTED):
        return "act"
    if permission == str(Permission.NEEDS_APPROVAL):
        return "mark_pending"
    return "draft"


def _after_approval(state: AgentState) -> str:
    status = (state.get("approval") or {}).get("status")
    return "act" if status in {str(s) for s in PROCEEDING} else "draft"


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
