# Concord

An autonomous support resolution agent for a payments platform's merchant support desk.

Concord reads an inbound merchant ticket, works out what it is about, pulls the facts from
the systems of record, checks the proposed resolution against written policy, obtains human
approval when the action carries risk, executes the action idempotently, and writes the reply —
leaving a replayable audit trail behind it.

It is designed to run **entirely inside the customer's network**. No merchant data leaves the
deployment. Inference is local by default, with a swappable gateway for customers who permit
hosted models.

---

## Why this exists

Most "support bot" projects demonstrate that a language model can answer a question. The hard
part of deploying one at a payments company is everything around that: integrating with systems
you do not control, encoding the customer's refund policy, knowing where autonomy has to stop,
surviving a gateway timeout in the middle of a refund, and proving in CI that last week's prompt
change did not regress scenario 9.

Concord is built around those problems. The agent is the small part.

---

## Design principles

**1. Determinism first.**
The model classifies, extracts, grounds and drafts. It never decides whether a refund is
permitted, never computes an amount, and never scores risk. Those are code paths with tests.

**2. Orchestration carries the reasoning load.**
Each graph node makes one narrow decision with a small context, so the system does not depend on
a single long chain of model reasoning being correct end to end.

**3. Every mutation is idempotent, scoped and logged.**
Tools are typed, carry idempotency keys, declare their authorization scope, and append to an
immutable run ledger before and after execution.

**4. Untrusted input stays untrusted.**
Merchant message content is data. It never reaches a position where it can be read as
instruction, and tool arguments derived from it are validated against the systems of record.

**5. A second customer is a config file.**
Tenant policy, thresholds, tone and knowledge sources are configuration. Onboarding a new
deployment must not require a code change.

---

## Architecture

```
            Intake API (FastAPI)
      chat, email, webhooks, async jobs
                    |
    +---------------v-----------------+
    |   Agent runtime (LangGraph)     |
    |   triage -> specialists ->      |
    |            verifier             |
    +---------------+-----------------+
                    |
   Policy engine | Human approval | Audit trail
                    |
              Tool layer
      typed, scoped, idempotent, retried
                    |
   CRM  |  Payments  |  Ticketing + knowledge base
```

| Plane | Responsibility |
| --- | --- |
| Intake | Accept tickets, enqueue runs, stream progress, expose run status |
| Agent runtime | Durable LangGraph thread per ticket; triage, specialist subgraphs, verifier |
| Governance | Policy evaluation, risk tiering, human approval gates, immutable audit log |
| Tool layer | Contract-bound clients with retries, circuit breaking, idempotency, authz scopes |
| Systems of record | Mocked CRM, payments, ticketing and knowledge services behind OpenAPI contracts |

---

## Repository layout

```
apps/
  api/          Intake and run-control API (FastAPI)
  console/      Reviewer console: approval queue and run trace viewer (Next.js)
  mocks/        Mock enterprise systems generated from the contracts
packages/
  agent/        LangGraph graph, nodes, state schema, prompts
  tools/        Contract-bound tool clients and the tool registry
  policy/       Policy engine, risk tiering, tenant configuration
  shared/       Domain types, money handling, error model, telemetry
evals/
  golden/       Golden scenario fixtures and expected assertions
  harness/      Eval runner, judges, reporting
contracts/      OpenAPI specs for the four systems of record
docs/
  adr/          Architecture decision records
  domain-model.md
  scenarios.md
  roadmap.md
infra/          Compose stack, migrations, local bootstrap
```

---

## Status

Phase 3 complete. Phase 4 next. See [docs/roadmap.md](docs/roadmap.md) for the full plan and
the definition of done for each phase.

| Phase | Title | State |
| --- | --- | --- |
| 0 | Foundations: contracts, domain model, scenarios | complete |
| 1 | Mock enterprise systems with fault injection | complete |
| 2 | Tool layer | complete |
| 3 | Vertical slice | complete |
| 4 | Knowledge grounding | next |
| 5 | Multi-agent graph | not started |
| 6 | Policy, risk tiering, human approval | not started |
| 7 | Failure engineering | not started |
| 8 | Evaluation and observability | not started |
| 9 | Tenant onboarding and deployment | not started |

---

## Getting started

Requirements: Docker, Python 3.12+, Node 20+, and Ollama running on the host.

```bash
cp .env.example .env
make bootstrap      # pull models, start infrastructure, run migrations
make doctor         # verify local inference and measure decode throughput
make check          # lint, types, contracts, money rule, tests
```

Submit a ticket and watch it resolve:

```bash
make up                                   # mock systems of record
SCENARIO=S01 make seed                    # load a scenario's state
uvicorn apps.api.main:app --port 8000 &   # the intake API

curl -s localhost:8000/tickets -H 'content-type: application/json' -d '{
  "merchant_id": "M-1001", "channel": "email",
  "subject": "Refund timing question",
  "body": "How long do refunds take to reach the customer'"'"'s account?"
}'
# -> {"run_id": "run_000001", ...}

curl -N localhost:8000/runs/run_000001/stream   # progress, as it happens
curl -s localhost:8000/runs/run_000001/steps    # the ledger for that run
```

Local inference is expected on the host rather than in a container so the GPU is reachable
without passthrough configuration. `make doctor` verifies the model is resident and reports the
measured decode rate, which the eval harness uses as its latency baseline.

---

## Reading order for reviewers

1. [docs/scenarios.md](docs/scenarios.md) — the twelve tickets that define scope
2. [docs/domain-model.md](docs/domain-model.md) — entities, states and money handling
3. [docs/adr/](docs/adr/) — why the system is shaped this way
4. [contracts/](contracts/) — the interfaces the agent is written against
