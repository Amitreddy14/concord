# ADR 0001: Orchestrate with LangGraph rather than a prompt chain or a bespoke loop

- **Status:** accepted
- **Date:** 2026-10-20
- **Phase:** 0
- **Deciders:** Amit Reddy

## Context

A merchant ticket resolution is not a single model call. It is a sequence of narrow decisions
with branches, retries, and — for anything that moves money — a pause that can last days while
a human approves it. Three properties follow from the domain rather than from taste:

1. **A run must survive process restart.** A refund awaiting a reviewer's approval cannot be
   held in memory. If the API restarts, the pending run must resume from where it paused with
   its accumulated facts intact.
2. **The reasoning must be decomposable.** Inference is local on an 8GB GPU (ADR 0003), so the
   resident model is 8B-class. An 8B model is reliable at one narrow judgement with a small
   context and unreliable across a long chain of dependent ones. The topology has to carry the
   structure that a frontier model would otherwise hold in its own reasoning.
3. **Every step must be inspectable after the fact.** "Why did it refund that leg?" has to be
   answerable from stored state, not reconstructed from logs.

Three options were on the table: a LangChain sequential chain, a hand-rolled state machine over
our own Postgres tables, or LangGraph.

## Decision

Build the runtime as a LangGraph `StateGraph` with a Postgres checkpointer, one durable thread
per ticket, and human approval implemented with `interrupt()` and resumption via
`Command(resume=...)`.

`interrupt()` suspends the graph at the exact point it is called, persists state through the
checkpointer, and waits indefinitely; resuming re-invokes the graph with the human's value
delivered back to the interrupt site. That is precisely the pause-for-approval semantics the
domain needs, and it is the single strongest reason for this choice over a plain chain.

## Consequences

**Accepted costs.**
- A framework dependency in the most important part of the system, with its own upgrade risk.
- Resumption re-executes the node containing the `interrupt` from its beginning, so node bodies
  must be written so that re-entry is safe. Any side effect placed before an `interrupt` in the
  same node will run twice. The rule in this repository: **a node that interrupts performs no
  mutation before the interrupt.** Mutations live in their own nodes, after the gate.
- Validation loops must be expressed as conditional edges. A `while True` around `interrupt()`
  causes exponential re-execution of the loop body on each resume, which is a documented
  failure mode and a lint-level prohibition here.

**Unlocked.**
- Durable multi-day approvals with no bespoke persistence code.
- Deterministic replay of a run from its checkpoints, which Phase 7 builds on.
- Per-node context budgets, which is what makes a small local model viable at all.

**Revisit when.** Checkpoint write latency exceeds roughly 50ms at p95 under the eval suite, or
when a graph needs genuine parallel fan-out across tenants that the checkpointer serializes.

## Alternatives considered

| Option | Why not |
| --- | --- |
| LangChain sequential chain | No durable pause. An approval gate would have to terminate the chain and start a second one, splitting the state and the audit trail across two runs. |
| Hand-rolled state machine on Postgres | Honest and dependency-free, but reimplements checkpointing, resumption and step replay. That is weeks of work on infrastructure that is not the differentiator. |
| Temporal / durable workflow engine | Correct semantics and arguably better durability, but a heavy operational dependency for a single-node deployment, and it pushes the LLM orchestration into activity code where the graph structure stops being legible. |

## Notes

LangGraph does not expire abandoned threads. A ticket whose approval never arrives would sit
pending forever, so thread TTL and abandonment handling are this application's responsibility
and are specified in Phase 6, not assumed from the framework.
