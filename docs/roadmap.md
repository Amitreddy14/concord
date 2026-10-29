# Roadmap

Ten phases. Each has deliverables and a definition of done; the generic phase gate is in
[CONTRIBUTING.md](../CONTRIBUTING.md). Scenario ids refer to [scenarios.md](scenarios.md).

---

## Phase 0 — Foundations

**Branch** `phase/0-foundations`

**Deliverables**
- Repository skeleton, Makefile, compose stack, environment template.
- `docs/domain-model.md` — entities, state machines, money, identity resolution, idempotency.
- `docs/scenarios.md` — the twelve scenarios.
- ADRs 0001–0006.
- `contracts/` — OpenAPI 3.1 specs for CRM, payments, ticketing, knowledge.
- `evals/golden/fixtures/` — machine-readable scenario fixtures.

**Done when** every spec validates, every scenario has a fixture whose referenced ids are
internally consistent, and the README reading order resolves.

---

## Phase 1 — Mock enterprise systems with fault injection

**Branch** `phase/1-mock-systems`

**Deliverables**
- Four FastAPI services implementing the contracts, with real state: a refund changes the
  capture it refunds.
- Seeded dataset loaded from the scenario fixtures.
- Fault-injection middleware: `off` \| `flaky` \| `hostile`, driven by `FAULT_SEED` so a profile
  reproduces the same failure sequence every run. Faults available: latency, 5xx, 429 with
  `Retry-After`, connection reset, and accept-then-timeout for S09.
- Idempotency store per service, returning the original response with `Idempotency-Replayed`.

**Done when** contract conformance tests pass against each mock, and S09's fault sequence is
reproducible from the seed.

**Verified.** 101 tests. Conformance is driven from the OpenAPI documents rather than restated,
so a new required field fails the suite until a service provides it. S09 runs five times from
its seed and reaches an identical outcome every time — one refund, correct amount and capture —
paired with a counter-test proving that the same sequence under fresh keys refunds twice, so the
guarantee cannot pass vacuously.

---

## Phase 2 — Tool layer

**Branch** `phase/2-tool-layer`

**Deliverables**
- Typed clients written against the contracts, Pydantic v2 at every boundary.
- A tool registry: name, argument schema, authorization scope, mutating flag, retry policy.
- Derived idempotency keys per the domain model.
- Retry with bounded exponential backoff and jitter; circuit breaker per dependency.
- Scope enforcement that reads the tenant configuration and ignores anything in the ticket.
- Ledger writes before and after every call.

**Done when** a mutating tool called twice with the same arguments inside one run produces one
effect, scope violations raise before any network call, and the circuit breaker opens and
recovers under the `hostile` profile.

**Verified.** 139 tests, run against the real mock services rather than stubs. A scope violation
is asserted by the ledger showing no scope used and the service holding no refund, not only by
the exception. S09 now passes end to end through the tool layer: two attempts, one refund, the
retry reporting a replay.

---

## Phase 3 — Vertical slice

**Branch** `phase/3-vertical-slice`

**Deliverables**
- `LLMGateway` over the OpenAI-compatible endpoint, with tool-support assertion at startup and
  enforced per-node context budgets.
- Schema-constrained decoding plus a bounded validate-and-repair loop.
- A minimal graph: triage → retrieve → draft.
- `POST /tickets` returning a run id, `GET /runs/{id}`, and SSE progress streaming.
- `make doctor`: verifies residency and reports measured decode throughput.

**Done when** S01 passes end to end against the mocks, and a resident-model restart does not
lose a queued run.

**Verified.** 162 tests. S01 runs through all four nodes with a cited reply and no mutating tool
call. A gateway failure classified retryable — the model being evicted mid-run — leaves the run
reaching the same end state. Untrusted containment is asserted by stripping every fenced region
from each assembled prompt and requiring the payload to be absent from what remains.

`make check` is also real for the first time: lint, format, `mypy --strict` on production code,
contracts, the money rule and the suite all run in CI.

---

## Phase 4 — Knowledge grounding

**Branch** `phase/4-knowledge-grounding`

**Deliverables**
- Policy and help-centre corpus for the tenant, chunked with stable ids.
- Hybrid retrieval: pgvector dense plus lexical, reciprocal-rank fusion.
- Citation enforcement: a claim without a supporting passage fails the draft check.
- Refusal path when the corpus does not support an answer.

**Done when** S01, S05, S07 and S08 produce cited replies, and a question with no supporting
passage yields a refusal rather than a fluent guess.

**Verified.** 204 tests. The four scenarios cite the real corpus by its real ids — the fixtures
no longer seed their own passages, so they exercise retrieval rather than standing in for it. An
unsupported question skips the draft node entirely and refuses. A fabricated citation is caught
end to end.

Three defects found while building it, each of which would have made the refusal path useless: a
dense retriever ranking every passage so nothing ever came back empty; a score normalised
against both retrievers even when only one contributed; and the word "merchant" being enough to
match a payments corpus.

---

## Phase 5 — Multi-agent graph

**Branch** `phase/5-multi-agent`

**Deliverables**
- Supervisor routing by intent to specialist subgraphs: account, transactions, disputes,
  settlements, knowledge.
- Entity resolution node implementing the domain model's resolution order, including the
  ambiguity branch.
- Verifier node checking a proposal against the facts it claims to rest on.
- Postgres checkpointer, one thread per ticket.

**Done when** S02, S07 and S11 pass, and the resolution order is covered by unit tests
independent of the model.

**Verified.** 252 tests. Resolution is pure functions with no model in the path, so the order is
checked directly — including that an amount matching the *older* payment selects it, which
recency alone would not. S11 terminates in a question with both candidates recorded and never
reaches the drafter. The durable checkpointer runs against a real Postgres in CI, with a guard
that fails the job if those tests silently skip.

---

## Phase 6 — Policy, risk tiering, human approval

**Branch** `phase/6-policy-and-approval`

**Deliverables**
- Policy engine evaluating tenant configuration: windows, caps by tier, forbidden actions,
  disclosure levels. Pure functions, unit tested, every decision recording a rule id.
- Risk tiering, and the mapping from tier to required approval.
- HITL via `interrupt()` and `Command(resume=...)`, with the no-mutation-before-interrupt rule
  enforced by a test that asserts it per node.
- Approval expiry and abandonment sweep — the framework does not expire threads, so this is ours.
- Reviewer console: approval queue, evidence panel, approve / reject / modify, run trace viewer.

**Done when** S03, S04, S05 and S08 pass, including S04's restart-while-pending and all three
decision branches.

**Delivered.** All four S04 branches rather than three — `expired` is a branch the fixture lists
and the original wording undercounted. The policy engine reads the required approval from the
tenant's tier table rather than computing it a second time; `ADR 0007` records why pending
approvals are indexed beside the checkpoint and swept from outside the graph; `ADR 0008` records
why the console duplicates the approval rules as user experience while the API keeps every one of
them as authority. The no-mutation-before-interrupt rule is asserted structurally as well as
behaviourally: a test parses the agent package and fails if any function both interrupts and calls
the tool invoker.

---

## Phase 7 — Failure engineering

**Branch** `phase/7-failure-engineering`

**Deliverables**
- Error taxonomy: retryable, fatal, needs-human, poisoned-input, with a mapping from every
  dependency failure mode.
- Compensating actions for partial failure, and a reconciliation pass that detects an action
  completed upstream after a local failure.
- Dead-letter queue with replay.
- Deterministic replay of a run from its checkpoints and recorded tool responses.

**Done when** S09 passes under the `hostile` profile across repeated seeded runs with exactly one
refund every time, and a replayed run reproduces its original decisions.

**Delivered.** Compensation turned out to have three strategies rather than one, and money has
none of them — the contract has six mutating tools and no inverse for any of them, so
`ADR 0009` records that the strategy for a refund is prevention, detection and escalation rather
than an undo. Reconciliation reports three outcomes, not two: `UNKNOWN` is never collapsed into
`ABSENT`, because the step after `ABSENT` is to try again. The dead-letter queue refuses to
replay a letter whose effect upstream is unknown, rather than warning about it.

Two defects surfaced while building it. The error taxonomy disagreed with `problem.py` about
whether `state_conflict` was retryable, and an unlisted 5xx fell through to `FATAL` — so a 507
was never retried, which is the case where a retry is most likely to work. Both are now covered
by a test that enumerates every problem code the services can emit rather than sampling.

---

## Phase 8 — Evaluation and observability

**Branch** `phase/8-evaluation`

**Deliverables**
- Eval harness: all twelve scenarios, hard assertions plus graded checks, `make eval` reporting.
- Model benchmark across the local shortlist on identical fixtures: pass rate, latency, tokens,
  VRAM headroom. Winner recorded as an ADR.
- OpenTelemetry traces spanning run, node and tool call; structured logs; per-run cost ledger.
- CI gate: the suite runs on every pull request and blocks on any hard-assertion failure.

**Done when** CI fails on an injected regression — a widened scope, a skipped gate, a dropped
citation — and the benchmark table is reproducible from a single command.

**Delivered.** The harness found five defects on its first full run, which was the point of
building it: nothing wrote back to the ticket and nothing created it, a duplicate charge refunded
the capture the buyer intended rather than the accidental one, `captures_touched` reported captures
a refund did not touch (tripping the multi-capture approval rule on a single-capture refund), and a
chargeback notice proposed a refund that would have paid the buyer twice. Two fixtures also
contradicted themselves — S01 and S12 each forbade a scope their own assertions required — and are
corrected with the reasoning recorded in the fixture.

CI runs the suite and the three injected regressions. The widened-scope regression needed the
check rewritten: it validated scopes against the registry, so when the regression changed a tool's
declared scope the expectation moved with it and the check passed. It now pins its own table.

`make benchmark` is the single command, and it is not in CI: it needs a GPU and minutes per model,
and a merge gate that depended on hardware would break when the hardware is busy. The model-choice
ADR is written from a run against the target card rather than from an assumption.

The benchmark then spent five rounds measuring the prompt rather than the models. Pass rates moved
from 5/10 to 9 and 10 out of 10, and not one of those movements came from a model — each was a
defect in the drafting instruction or in the harness itself, two of the five being assertions that
disagreed with the contracts they were checking. The signal that separated the two was the failure
*signature*: two unrelated 8B models failing the same assertion the same way is evidence about the
instruction, and models failing differently is evidence about the models. `ADR 0010` records the
choice and is explicit about how little ten assertions can carry.

One round made both models worse, which is the finding most worth keeping. A clarifying paragraph
that correctly disambiguated `unsupported_claims` carried the vocabulary of dispute adjudication,
and qwen3 answered a settlement ticket by adjudicating a defective product that does not exist in
the scenario, inventing two record ids to cite. At this size an instruction is not a specification
the model consults; it is context competing with the task. The instruction is now shorter than
before the clarification.

That near miss produced the one piece of hardening Phase 8 did not plan for. `ADR 0005` rule 3
re-validates every identifier arriving *from* merchant text, in four places — and nothing
re-validated the identifiers the model sends *out*. The invented records were caught only because
they were also cited; named in prose and omitted from the citations array, they would have reached
a merchant. `grounding.verify` now refuses a reply naming an identifier that appears in neither
the facts nor the corpus.

---

## Phase 9 — Tenant onboarding and deployment

**Branch** `phase/9-tenant-onboarding`

**Deliverables**
- A second tenant added as configuration only, with different caps, windows, tone and corpus.
- A test asserting no tenant-specific branch exists in `packages/`.
- Onboarding runbook and a deployment guide for an air-gapped environment.
- Demo script walking S02, S04, S09 and S10 in order.
- Project write-up: architecture, decisions, measured results, known limits.

**Done when** the second tenant passes the suite with its own expectations, and a reader can
bring the system up and run the demo from the README alone.

**Delivered.** `northwind-pay` is a second tenant in configuration only — 90-day window against
180, caps an order of magnitude lower in EUR, and `duplicate` as the only reason code that
releases a refund without a human. A conformance suite decides ten policy cases and four scope
cases against both tenants, with every expectation hand-written from reading the tenant files
rather than recorded from the engine, and a divergence table requiring each configured difference
to change an outcome somewhere. An AST walk over `packages/` fails if a tenant id, display name or
sign-off appears as code.

Writing the deliverables found more than building them did. The no-tenant-branch guard was a grep
first and fired on the docstring explaining the bug it guards — the same mistake `check_money.py`
was rewritten to avoid. Two rule constants turned out to be vestiges of the design Phase 6
replaced, defined and unreferenced for three phases, naming the superseded approach where the
engine decided approval itself. Two test fixtures were passing while silently taking an exception
fallback, because they spelled a money payload key wrong.

And the narrated walkthrough exposed, on its first run, a defect 541 tests could not see: a
merchant whose refund a reviewer declined was sent "Thanks for getting in touch. Let me know if
anything is unclear." The branch asserted the refund had not happened, the ticket had escalated,
and the reviewer's note had reached the ticket. All three passed. None of them was about the
merchant. `gate._approval_outcome` had already separated rejected from expired on the stated
grounds that they are "different things to tell a merchant" — and then nothing told the merchant
either.

The README was missing the step that creates the virtualenv, which is the one Phase 9's done-when
depends on most directly.
