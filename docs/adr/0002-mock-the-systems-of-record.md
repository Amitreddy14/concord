# ADR 0002: Build the systems of record as contract-first mocks

- **Status:** accepted
- **Date:** 2026-10-20
- **Phase:** 0
- **Deciders:** Amit Reddy

## Context

The agent needs a CRM, a payments ledger, a ticketing system and a knowledge base. None of them
exist. The options were to use a real vendor sandbox (Razorpay test mode, a Zendesk trial), to
stub the calls inside the tool layer, or to build mock services behind explicit contracts.

The deciding consideration is that the interesting engineering in this project lives in failure
handling. Scenario S09 — the payments API times out after the refund has already been accepted
upstream — cannot be demonstrated against a vendor sandbox, because a sandbox will not time out
on command. Neither can rate limiting, duplicate responses to a retried mutation, or a 500 in
the window between two writes. If those cannot be triggered deterministically, they cannot be
tested, and untested failure handling is decoration.

## Decision

Define each system of record as an OpenAPI 3.1 contract in `contracts/`, and implement each as
a small FastAPI service in `apps/mocks/` that serves that contract. Every mock sits behind a
shared fault-injection middleware driven by `FAULT_PROFILE` and a seeded RNG, so a named
scenario reproduces the same failure sequence on every run.

The tool layer in `packages/tools/` is written against the contracts, never against the mock
implementations.

## Consequences

**Accepted costs.**
- Phase 1 is spent building systems that are not the product.
- A mock will diverge from any real vendor's behaviour in ways that are invisible until a real
  integration happens. The contracts are therefore modelled on documented real-world
  behaviour — RFC 9457 problem responses, `Idempotency-Key` on mutations, `Retry-After` on 429 —
  rather than on whatever is convenient to implement.

**Unlocked.**
- Deterministic, seeded reproduction of every failure scenario, which makes Phase 7 and the CI
  eval gate possible.
- No external network dependency, so the whole system runs air-gapped — which is the deployment
  story the project is arguing for (ADR 0003).
- Swapping a mock for a real client later is a tool-layer change behind an unchanged contract.

**Revisit when.** A real integration target is chosen. At that point the contract becomes an
adapter boundary and the mock becomes a test double.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Vendor sandboxes | Cannot inject faults on demand, needs network and credentials, and makes the eval suite non-deterministic and rate-limited. |
| In-process stubs in the tool layer | The tool layer would then be tested against itself. Retries, circuit breaking and idempotency are only meaningfully exercised across a real HTTP boundary. |
| `prism mock` from the OpenAPI spec | Good for shape conformance, but it cannot model state — a refund must change the transaction it refunds, and the duplicate-capture scenario needs a stateful ledger. |
