# ADR 0007: Index pending approvals beside the checkpoint, and sweep them ourselves

- **Status:** accepted
- **Date:** 2026-10-22
- **Phase:** 6
- **Deciders:** Amit Reddy

## Context

ADR 0001 chose LangGraph for one property above the rest: a run can stop mid-graph to wait for a
human and survive the process restarting. Phase 6 is where that property is actually used, and
using it surfaced two things the checkpoint does not do.

A checkpoint is addressed by thread id. Given a ticket it will say exactly where that ticket's run
is paused, which is the hard part and is genuinely solved. But a reviewer console does not arrive
holding a ticket id. It asks *what is waiting for a human, across every ticket*, and answering
that from checkpoints alone means enumerating threads and inspecting each one's pending tasks —
not a query, and worse as the deployment grows.

The second gap is sharper. A frozen thread stays frozen forever. Nothing in LangGraph expires it,
nothing escalates it, and a paused run is indistinguishable from the inside after four minutes and
after four weeks. The failure mode is not an error: it is silence. A merchant waits on a refund,
the approval queue looks healthy because the row is still `pending`, no exception is raised
anywhere, and nothing a dashboard watches changes. The tenant configuration has said
`expires_after_hours: 48` since phase 0, and that number was not going to enforce itself.

There was a tempting third option worth naming because it is wrong in an instructive way: store the
approval state *in* the graph state and drive expiry from inside the graph — a node that loops on
`interrupt` with a timeout. It cannot work. A paused graph executes no code, so there is nothing
running to notice the deadline, and a loop around `interrupt` re-executes its own body on every
resume (which is why ADR 0001 forbade that shape).

## Decision

Keep a queryable index of approval requests beside the checkpoint, and run an expiry sweep from
outside the graph. The index holds no authority: the decision it carries was made by the policy
engine, the pause it points at lives in the checkpoint, and the resume is performed against the
graph. Where the two disagree, the checkpoint is right — the index is a way of finding threads and
deadlines, not a second source of truth. A reviewer's decision is recorded in the index *before*
the run is resumed, and `await_approval` re-reads the index rather than trusting its resume
payload, so a crash between the two loses nothing and an approval id arriving over HTTP
authorises nothing.

## Consequences

**Accepted costs.** Two stores that can drift, and a resume path that must tolerate being run
twice. Both are paid for by construction: approval rows are keyed by `(run_id, proposal
fingerprint)` and upserted, because LangGraph re-executes an interrupted node from its first line
and a plain insert would produce a second row on every resume. Terminal states are terminal, so a
reviewer's answer is never overwritten by a later sweep, and the sweep moves only `pending` rows —
the race between a reviewer clicking approve and the sweep expiring the same request is real, and
whoever gets there first wins. There is also now a background loop in the API that must keep
running; if it dies quietly the queue silently stops timing out, which is the exact failure this
ADR exists to prevent, so it is exposed as `POST /approvals/sweep` as well.

**Unlocked.** A reviewer console with a real queue, sorted oldest-first and showing time to
expiry. An audit trail that holds both what the agent proposed and what the human allowed, which
a store keeping only the final figure could not answer. S04's expired branch becomes testable in
milliseconds by moving a deadline rather than mocking a clock.

**Revisit when.** LangGraph grows a cross-thread query for pending interrupts *and* a
time-to-live on them. The first alone is not enough: the queue is the convenience, the expiry is
the correctness problem.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Enumerate threads to build the queue | Not a query; cost grows with total tickets, not pending ones. And it still leaves expiry unsolved. |
| Expire from inside the graph | A paused graph runs no code, so nothing is there to notice the deadline. A loop around `interrupt` also re-executes its body on every resume (ADR 0001). |
| Make the index authoritative and drop to it entirely | Then a resumed run's accumulated facts have to be rebuilt from the index, which is the work the checkpointer already does correctly. |
| Trust the resume payload instead of re-reading | The payload is the one input here that an attacker can shape. An approval id must identify a decision, never constitute one. |
| Let a reviewer raise the approved amount | The proposal was evaluated against the capture's remainder and the tenant's window; a larger figure has been checked against nothing and would arrive at the mutating node carrying an approval that appears to authorise it. Reviewers may reduce; raising means refusing and letting the agent re-evaluate. |
