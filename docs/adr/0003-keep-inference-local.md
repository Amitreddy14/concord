# ADR 0003: Keep inference local and treat the model as a swappable gateway

- **Status:** accepted
- **Date:** 2026-10-20
- **Phase:** 0
- **Deciders:** Amit Reddy

## Context

A payments company's merchant support tickets contain cardholder-adjacent data, settlement
figures and KYC status. Shipping that to a third-party inference API raises data residency and
vendor-processing questions that, in a real deployment conversation, are decided by a compliance
team rather than by an engineer. A system that cannot run without egress is a system that cannot
be deployed in those environments at all.

The development hardware is an RTX 4060 with 8GB of VRAM and 16GB of system RAM shared with
Postgres and a Next.js dev server. That is the binding constraint on model size: roughly 5GB of
weights is the ceiling for a Q4 8B-class model once KV cache is accounted for, and swapping
models between nodes thrashes VRAM badly enough to dominate run latency.

## Decision

Default to local inference through an OpenAI-compatible gateway (Ollama on the host, so the GPU
is reachable without container passthrough), with one resident model serving every node. The
provider, base URL and model names are configuration; `packages/shared` exposes a single
`LLMGateway` interface and no node constructs a client of its own.

The model is kept resident with a long `keep_alive`, temperature is pinned at 0 with a fixed
seed, and every node declares a context budget that is enforced in code rather than hoped for.

## Consequences

**Accepted costs.**
- Quality ceiling. An 8B model at Q4 is meaningfully weaker than a frontier model at extraction
  and at drafting tone. The architecture compensates: the model never decides policy (ADR 0004),
  outputs are schema-constrained and validated with a bounded repair loop, and the verifier node
  checks the proposal against deterministic rules.
- Throughput. At roughly 25–30 tokens/second decode on this class of hardware, a resolution takes
  tens of seconds. The API is therefore asynchronous from Phase 3 — submit returns a run id, and
  progress is streamed — rather than request/response.
- One resident model means no cheap-model-for-triage optimisation. `LLM_MODEL_TRIAGE` exists in
  configuration but defaults to the primary model; splitting it is a measured decision for
  Phase 8, not an assumption.

**Unlocked.**
- Air-gapped deployment, which is the project's central deployment claim.
- Zero marginal cost per eval run, so the golden suite can run on every commit in CI.
- A hosted model becomes a one-line configuration change for a customer who permits it, and the
  eval harness can compare the two on identical fixtures.

**Revisit when.** The Phase 8 benchmark shows the resident model failing scenario assertions
that a 14B-class or hosted model passes, and the deployment target permits the larger option.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Hosted frontier API as the default | Better quality, but it makes the system undeployable in the environment the project is designed for, and every eval run costs money, which discourages running them. |
| Two local models, small for triage and larger for resolution | 8GB cannot hold both. Alternating them reloads weights between nodes and the reload dominates latency. |
| Hybrid: local classification, hosted resolution | Still egresses the ticket body, which is the data that matters. It gets the compliance cost without the quality benefit. |

## Notes

Model selection is deliberately not fixed here. The shortlist is benchmarked against the golden
scenarios in Phase 8 and the winner is recorded in a follow-up ADR. Two practical notes for that
exercise: models without tool-calling support fail silently rather than erroring, so the gateway
asserts tool support at startup; and the candidate set is bounded by what fits alongside the KV
cache at the configured context length, not by what the registry says the download size is.
