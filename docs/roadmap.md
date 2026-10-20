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

---

## Phase 2 — Tool layer

**Branch** `phase/2-tool-layer`

**Deliverables**
- Typed clients generated against the contracts, Pydantic v2 at every boundary.
- A tool registry: name, argument schema, authorization scope, mutating flag, retry policy.
- Derived idempotency keys per the domain model.
- Retry with bounded exponential backoff and jitter; circuit breaker per dependency.
- Scope enforcement that reads the tenant configuration and ignores anything in the ticket.
- Ledger writes before and after every call.

**Done when** a mutating tool called twice with the same arguments inside one run produces one
effect, scope violations raise before any network call, and the circuit breaker opens and
recovers under the `hostile` profile.

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
