# ADR 0004: The model never decides policy, computes money, or scores risk

- **Status:** accepted
- **Date:** 2026-10-20
- **Phase:** 0
- **Deciders:** Amit Reddy

## Context

The default shape of an agent project is to describe the business rules in the system prompt and
let the model apply them. That design has two problems that are fatal in a payments context.

First, it cannot be tested. "Is a refund outside the 180-day window correctly refused?" becomes a
question about a probability distribution rather than about a branch, so the answer can change
when an unrelated prompt is edited.

Second, it cannot be audited. A reviewer asking why a ₹41,200 refund was approved needs the rule
that permitted it and the inputs it was evaluated against, not a paraphrase generated after the
fact.

The local 8B model (ADR 0003) makes this worse, but the argument does not depend on model size.
A frontier model asked to apply a refund cap is still an unauditable arithmetic unit.

## Decision

Partition the work by whether it is a judgement about language or a judgement about rules.

The model may: classify intent, extract entities as a validated schema, decide which system of
record to consult, rank and ground passages from the knowledge base, draft prose, and judge
whether a drafted reply is supported by its citations.

The model may not: determine whether an action is permitted, compute or adjust a monetary
amount, assign a risk tier, decide whether human approval is required, or choose an authorization
scope. Those are pure functions in `packages/policy/` and `packages/shared/money.py`, with unit
tests, evaluated against a tenant configuration file.

Where the model proposes an action, it proposes it as a typed object. The policy engine is what
turns a proposal into a permitted action, a refusal, or an approval request.

## Consequences

**Accepted costs.**
- More code. Every rule is a function and a test rather than a sentence in a prompt.
- Less flexibility at the edges. A ticket that needs a judgement the rule set does not cover is
  escalated to a human instead of being improvised. For this domain that is the correct failure
  mode, and S12 exists to prove it happens cleanly.

**Unlocked.**
- Refusals and approvals are reproducible, so they can be asserted in CI deterministically
  rather than judged by a second model.
- An 8B local model becomes viable for a task that would otherwise need a frontier model.
- The audit trail records the rule id and the inputs for every decision, which is what makes the
  reviewer console meaningful rather than decorative.
- Money handling is centralised: integer minor units with an ISO 4217 code, floats banned by a
  lint rule in any path that touches an amount.

**Revisit when.** A tenant's policy becomes genuinely open-ended enough that enumerating rules is
no longer tractable. The response then is a rule DSL, not moving the decision into the prompt.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Policy in the system prompt | Untestable, unauditable, and silently sensitive to unrelated prompt edits. |
| Model decides, code validates afterwards | Better, but the model still produces the amount, so the validator has to re-derive the correct answer to check it — at which point the validator is the policy engine and the model call is redundant. |
| Model-generated rule code, reviewed by a human | Interesting for authoring tenant configuration, out of scope for the runtime path. |
