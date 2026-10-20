# Working agreement

This repository is built in phases. The history is meant to be read, so the commit log and the
pull request descriptions are deliverables, not bookkeeping.

---

## Local setup

Git hooks live in `infra/githooks/` so they are version controlled and visible, rather than
hidden in `.git/hooks` where a clone would never see them. Git does not pick that path up on its
own, so point it there once per clone:

```bash
make hooks        # equivalently: git config core.hooksPath infra/githooks
```

The `pre-push` hook refuses a direct push to `main`. GitHub's own rulesets do not enforce on a
private repository under a free account, so until this repository goes public the hook is the
guardrail. A deliberate override is `git push --no-verify`.

---

## Branching

Trunk is `main` and is always green.

Each phase gets one branch, named for the phase:

```
phase/0-foundations
phase/1-mock-systems
phase/2-tool-layer
...
```

Work lands on the phase branch in small commits, then the phase branch is merged into `main`
with a merge commit (not squashed) so the shape of the work stays visible. The pull request
description for a phase is the phase write-up: what was built, what was decided, what was
deferred, and how the definition of done was verified.

Hotfixes to a merged phase use `fix/<short-slug>` off `main`.

---

## Commit messages

Conventional Commits, with a scope that names the package or app touched.

```
<type>(<scope>): <imperative summary under 72 characters>

<body: why, not what. wrap at 80.>

<footer: refs, breaking changes>
```

Types in use:

| Type | Use for |
| --- | --- |
| `feat` | New capability in a package or app |
| `fix` | Corrected behaviour |
| `docs` | Documentation, ADRs, scenario catalogue |
| `test` | Eval fixtures, unit and integration tests |
| `refactor` | Behaviour-preserving restructuring |
| `perf` | Latency, token or memory improvements |
| `build` | Compose, Makefile, dependencies, CI |
| `chore` | Repository mechanics with no product effect |

Scopes: `agent`, `tools`, `policy`, `shared`, `api`, `console`, `mocks`, `evals`, `contracts`,
`infra`, `docs`.

Examples:

```
feat(tools): add idempotency keys to all mutating payment tools

A retried refund must not create a second refund. Keys are derived from
the run id and the tool call index so a replayed graph step produces the
same key, and the mock payments service returns the original response.

Refs: docs/scenarios.md#s09
```

```
docs(adr): record decision to keep inference local by default
```

Rules:

- One logical change per commit. A commit that touches the policy engine and the console is two
  commits.
- Never commit a broken `main`. Broken intermediate states on a phase branch are fine and
  honest.
- Reference the scenario id (`S01`–`S12`) in the body whenever a change exists because of a
  specific scenario. That trail is the main argument that the project was built against
  requirements rather than toward a demo.

---

## Definition of done for a phase

A phase is not finished until all of these hold:

1. Its deliverables in `docs/roadmap.md` exist and are reachable from the README.
2. `make check` passes: lint, type-check, contract validation, tests.
3. New behaviour is covered by at least one eval fixture or unit test.
4. Any decision that was non-obvious at the time is recorded as an ADR.
5. The README status table is updated.

---

## Architecture decision records

Any decision that a future reader would otherwise have to reverse-engineer gets an ADR in
`docs/adr/`, numbered sequentially, using the template in `docs/adr/0000-template.md`.

Supersede rather than edit: a decision that changes gets a new ADR that marks the old one
superseded, and the old file stays.

---

## Code conventions

- Python: `ruff` for lint and format, `mypy --strict` on `packages/`, Pydantic v2 for every
  boundary type. No bare `dict` crossing a module boundary.
- TypeScript: `biome`, strict mode, no `any` outside generated clients.
- Money is always integer minor units plus an ISO 4217 currency code. Floats are banned in any
  path that touches an amount; this is enforced by a lint rule, not by discipline.
- Times are UTC, stored as timezone-aware, serialized as RFC 3339.
- Every outbound call carries a correlation id propagated from the run.
