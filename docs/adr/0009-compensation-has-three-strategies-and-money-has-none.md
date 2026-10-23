# ADR 0009: Compensation has three strategies, and money has none of them

- **Status:** accepted
- **Date:** 2026-10-23
- **Phase:** 7
- **Deciders:** Amit Reddy

## Context

Phase 7's roadmap entry asks for "compensating actions for partial failure". The phrase carries
an assumption worth checking before building to it: that a partial failure can be compensated.

The contract has six mutating tools. Not one of them has an inverse. There is no
`cancel_refund`, no `delete_note`, no `unsend_message`. This is not an oversight in the mock —
it is what the real systems look like. A card refund that has been accepted by the gateway is a
settlement instruction, and the instruction to reverse it is a different financial event with
its own rules, not an undo button.

So a literal reading of the deliverable produces a `compensate()` function that works for the
cheap cases and silently fails for the expensive one. That is worse than not having it, because
its existence implies a safety net that is not there, and the first person to rely on it will do
so during an incident.

The second thing that forced a decision is narrower and sharper. After an indeterminate failure
on a mutation — a timeout after the request was accepted — the run does not know whether the
effect landed. Finding out requires reading the system of record, and that read can itself fail.
Three outcomes, then, not two: it landed, it did not, or we still do not know. The temptation is
to fold the third into the second, because "we could not confirm it happened" sounds like "it did
not happen". It is not: the step after "it did not happen" is to try again.

## Decision

Name three compensation strategies, assign one to every mutating tool explicitly, and let the
honest answer for money be that there is none.

`RESTORE` for tools that set a value — ticket status, assignment, flags — where the compensation
is setting it back, and the prior value must have been captured before the change. `CORRECT` for
append-only records, where a note cannot be unwritten and a second note saying the first was
wrong is the honest repair, leaving both visible. `NONE` for `create_refund`, where no inverse
exists and saying so is more use to an operator than a function that fails quietly.

A tool absent from the table raises rather than defaulting to `NONE`, because "nobody decided"
and "no inverse exists" are different facts and only one of them is acceptable.

For the indeterminate case, reconciliation reads the system of record and reports `LANDED`,
`ABSENT` or `UNKNOWN`, and `UNKNOWN` is never collapsed into `ABSENT`. A run that ends `UNKNOWN`
goes to the dead-letter queue, which refuses to replay it until a person has established what
happened upstream.

## Consequences

**Accepted costs.** Some failures end with a human and no automated resolution, which is a worse
headline number than a system that retries everything. The number it improves is the one that
matters: duplicate refunds. There is also a table to maintain, and a new mutating tool is a
decision someone has to make rather than inherit — enforced by a test that compares the table
against the registry in both directions, so a tool added without a strategy fails and a strategy
left behind after a tool stopped mutating fails too.

**Unlocked.** `NONE` is tolerable only because of what phase 6 built: a refund above the tenant's
cap never happens without a human, so the unrecoverable case is narrow by construction rather
than by luck. The three-outcome reconciliation also gives the dead-letter queue something
precise to refuse on, rather than a flag somebody sets by hand.

**Revisit when.** The payments contract grows a reversal endpoint. At that point `create_refund`
moves from `NONE` to a real compensation and the dead-letter queue's refusal narrows to the
cases where even the reversal cannot be confirmed.

## Alternatives considered

| Option | Why not |
| --- | --- |
| One `compensate()` per tool, failing for refunds | Implies a safety net that is not there. The first person to rely on it will do so mid-incident. |
| Treat UNKNOWN as ABSENT and retry | The retry is the duplicate refund. This is the single most expensive mistake available in this system. |
| Treat UNKNOWN as LANDED and report success | Tells a merchant their money is coming when it may not be, and closes the run so nobody checks. |
| Reconcile by re-issuing the mutation | The idempotency replay would answer the question — by performing the effect when the answer is "it did not happen", at a moment when nobody has decided it should happen again. |
| Match the upstream effect on `(capture_id, amount)` | Adopts another run's refund for the same figure as this run's. A worse error than the one being fixed, and silent. |
| Drain the dead-letter queue automatically | A retry loop with extra storage. These failures turn on facts outside the system — whether the gateway's incident is over, whether someone already refunded by hand. |
