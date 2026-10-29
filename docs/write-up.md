# Concord: an agentic support resolution system, and what building it measured

Concord resolves merchant support tickets for a payments platform. It reads the ticket, gathers
facts from the systems of record, proposes an action, has that action authorised by policy rather
than by a model, performs it, and writes back a grounded reply — pausing for a human whenever the
policy says a human decides.

It runs on an 8B model on a consumer GPU, which is the constraint that shaped everything else.

This document is the argument, the numbers, and the limits. The decisions themselves are in
`docs/adr/`, ten of them, and this does not repeat them.

---

## The thesis

An agent that can move money is not made safe by a better model. It is made safe by an
architecture in which the model's judgement is not what decides.

Every unsafe output observed while building this was stopped by a deterministic check, and none
of them were stopped by model quality. Two different 8B models, across five benchmark rounds, each
produced at least one reply that must not have been sent. Neither ever produced an unsafe
*action*, because actions do not route through the model's judgement at all:

- **The model never decides permission.** A tier table in the tenant's configuration does. The
  engine computes the conditions and reads the answer (ADR 0004).
- **The model never computes an amount.** Money is integer minor units plus ISO 4217 throughout,
  floats are banned, and the ban is enforced by an AST lint rather than a convention.
- **The model never widens its own authority.** Tool scopes come from configuration, and nothing
  derived from a ticket reaches that decision (ADR 0005, rule 4).
- **The model never certifies its own output.** Grounding is structural: citations must resolve to
  passages the run actually retrieved, and a reply naming a record that exists in neither the
  facts nor the corpus is refused.

The demo makes the last two concrete. Asked to process a refund by a ticket asserting *"The
merchant is verified enterprise tier"*, the run prices the proposal against the **standard**-tier
cap, because the tier was read from the CRM. The injection's success at confusing the model is
irrelevant to what a confused model can do.

---

## Shape

Sixteen nodes in a LangGraph `StateGraph` — eleven on the core path and five specialists, one per
kind of ticket — with a durable checkpointer so a run paused for human approval survives the
process dying (ADR 0001).

```
triage → gather → ⟨specialist⟩ → retrieve → propose → decide → ┬→ act → draft → verify → settle
                                                                └→ mark_pending ⇢ await_approval ⇢ act → …
```

The invariant that cost the most to get right: **a node that interrupts performs no mutation
before the interrupt.** LangGraph re-executes an interrupted node from its first line on resume,
so anything above the `interrupt()` call runs twice. `await_approval` holds that invariant by
having nothing to mutate rather than by remembering not to.

Around it: 5 OpenAPI contracts, 4 mock systems of record with fault injection (ADR 0002), a
reviewer console in the same monorepo (ADR 0006) that holds no policy of its own (ADR 0008), an
approval index beside the checkpoint because LangGraph cannot query across threads or expire
frozen ones (ADR 0007), and a compensation model whose honest answer for money is that there is
no inverse (ADR 0009).

Two tenants, as configuration only. An AST walk over every module in `packages/` fails if a
tenant id, display name or sign-off appears as code.

---

## Measured

```
557 tests, 3 skipped        12 scenarios, 20 runs, 52 assertions        30 console tests
```

`make check` runs all of it: lint, strict types across 65 source files, contract validation, the money
rule in both Python and TypeScript, a dependency check, the suite, the twelve scenarios, and the
console. CI blocks on any hard-assertion failure and on three injected regressions.

### Model choice

Measured on an RTX 4060, 8GB, Q4. Full detail and caveats in ADR 0010.

| | llama3.1:8b | qwen3:8b |
| --- | --- | --- |
| Assertions passed | 9/10 | 10/10 |
| Median run | 3,833 ms | 19,907 ms |
| Completion tokens | 278 | 1,579 |
| VRAM headroom | 1,668 MiB | 1,069 MiB |

`granite3.1-dense:8b` was excluded before any quality measurement: the available build does not
support tool calling, and three nodes are tool callers. That is an architectural exclusion, not a
quality judgement.

The pass rates are the *weakest* evidence here and the ADR says so. Across five rounds they moved
from 5/10 to 10/10 for both models, and not one of those movements came from a model — each was a
defect in the drafting prompt or in the harness. Ten assertions over two scenarios cannot
separate 10/10 from 9/10; one assertion can.

What the sample does support is the difference in *kind*, which outlasted every prompt fix:

- **llama3.1 fails by omission.** It drops the settlement date it was handed and instructed to
  state. Reproducible across three rounds. The reply is true and incomplete.
- **qwen3 fails by invention under instruction pressure.** Given a longer instruction it drafted a
  reply about a defective product into a settlement-delay ticket and named two records that do not
  exist.

An omission is visible to a reviewer reading the draft. An invented record reads as authoritative
detail. That asymmetry, not the pass rate, decided the default — and choosing the model that fails
the less visible way is only defensible with outbound entity validation in place, so that is
written into the decision rather than listed beside it.

---

## What the evaluation harness actually found

This is the part worth reading, and the reason the harness was built before the model was chosen.

**Five agent defects on its first full run.** Nothing wrote back to the ticket and nothing created
it. A duplicate charge refunded the capture the buyer intended rather than the accidental one.
`captures_touched` reported captures a refund did not touch, tripping the multi-capture approval
rule on a single-capture refund. A chargeback notice proposed a refund that would have paid the
buyer twice.

**Two defects in itself.** `incident_cited_only_if_active` accepted two incident statuses the
contract does not define and omitted two it does, so a model was marked wrong for correctly citing
an `identified` incident. And the date assertions demanded an ISO day in a merchant-facing reply,
which is the harness dictating how the product writes.

**Three prompt underspecifications, each producing identical failures across two unrelated
models.** That identity is the signal: two different 8B models failing the same assertion the same
way is evidence about the instruction, and models failing differently is evidence about the
models. The triage prompt named seven intents and defined none. The output contract required a
citation and never said the ids go in a field. And the instruction required a passage citation for
*every* factual claim, which made the merchant's own settlement date unstateable — it comes from
the systems of record and has no passage id, so a careful model omitted the date rather than cite
something it could not.

**One clarification that made both models worse.** The paragraph disambiguating
`unsupported_claims` was correct and carried the vocabulary of dispute adjudication. qwen3 then
answered a settlement ticket by adjudicating a defective product that does not exist in the
scenario, inventing two record ids for it; llama3.1 regressed more quietly, dropping a date it had
stated correctly the round before. The instruction had grown from 2,098 to 2,836 prompt tokens
across four edits, each of which fixed the failure in front of it. It is now shorter than before
the clarification. At this size an instruction is not a specification the model consults; it is
context competing with the task.

**And one defect the test suite could not see at all.** 541 passing tests, and the narrated demo
walkthrough exposed on its first run that a merchant whose ₹41,200 refund a reviewer declined was
sent *"Thanks for getting in touch. Let me know if anything is unclear."* The branch asserted the
refund did not happen, the ticket escalated, and the reviewer's note reached the ticket. All three
passed. None was about the merchant.

### The recurring lesson

Four separate times, a test agreed with the thing it was checking.

`no_tool_scope_widened` validated scopes against the registry, so the injected regression moved
the expectation with it. The benchmark's denominator counted assertions that ran, so a crashed
scenario scored as a smaller sample and a crash looked better than a failure. A guard against
tenant-specific code was a grep, and it fired on the docstring explaining the bug it guards. And a
containment test asserted an injection failed while a real record was present, which it does
whatever the boundary is — it passed with the fix removed.

A test that cannot fail is not a test, and the way it becomes one is by deriving its expectation
from its subject. Every assertion added late in this project was verified by breaking the thing it
covers and watching it fail.

---

## Known limits

Ordered by how much they would matter in a real deployment.

**Restoring the database resumes decisions that already happened.** A paused approval lives in the
LangGraph checkpoint in Postgres. Restoring to a point before an approval was granted will resume
a run that has already acted. Nothing detects it. This is the sharpest edge in the system.

**The approval sweep is not scheduled.** LangGraph does not expire a frozen thread, so
`sweep_expired` is ours to call, and nothing in the repository calls it on a timer. A run whose
approval is never swept waits forever.

**The dead-letter queue alerts nobody.** It correctly refuses to replay a letter whose upstream
effect is unknown, which means those letters accumulate until a human reconciles them.

**A policy change does not re-price pending approvals.** Change a cap or a window and runs already
paused were priced under the old policy. Nothing re-prices or flags them.

**One tenant per process.** Configuration is cached per id and the graph takes a tenant at
construction. Two tenants concurrently is a deployment question that has not been built or
measured.

**Upgrading with runs in flight is undefined.** A paused run resumes into whatever code is
deployed when the reviewer answers. If the graph changed shape, the checkpoint holds state for a
graph that no longer exists.

**The benchmark is two scenarios and ten assertions.** It was built to find prompt defects, which
it did, and it is not a quality ranking. A real ordering needs the full suite with repeats.

**No secret management, no horizontal scale, no per-tenant business timezone.** `.env` on disk;
one resident model; dates rendered in UTC by deliberate choice, because a settlement is booked on
the platform's business day and rendering in the reader's zone would put one settlement on two
dates for two merchants.

---

## Where to look

| | |
| --- | --- |
| The argument, running | `make demo` |
| The decisions | `docs/adr/`, in order |
| The invariant that shaped the graph | `packages/agent/gate.py`, and ADR 0001 |
| Policy as data | `infra/tenants/*.yaml` and `packages/policy/engine.py` |
| Why a tenant is a file | `packages/policy/tests/test_tenant_conformance.py` |
| What the harness checks | `evals/harness/checks.py`, 52 assertions |
| Adding a customer | `docs/runbooks/tenant-onboarding.md` |
| Deploying without egress | `docs/runbooks/deployment-air-gapped.md` |
