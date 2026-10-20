# ADR 0006: One repository, phase branches, and a commit history meant to be read

- **Status:** accepted
- **Date:** 2026-10-20
- **Phase:** 0
- **Deciders:** Amit Reddy

## Context

This project has a secondary audience: an engineer who opens the repository to judge how its
author works. That audience reads the README, the ADR directory and the commit log, usually in
that order, and usually for a few minutes.

Two structural choices follow. First, where the reviewer console lives. A separate repository is
cleaner for independent deployment, but it splits the demo across two clones and two setup paths,
and it hides the fact that the console and the API share a domain vocabulary. Second, how
history is shaped: a squash-merge workflow produces a tidy log of nine commits that says nothing
about how the work was actually done.

## Decision

One repository containing `apps/` (api, console, mocks), `packages/` (agent, tools, policy,
shared), `contracts/`, `evals/`, `docs/` and `infra/`. A single `docker compose up` plus
`make bootstrap` brings up the whole system including the console.

Each phase is developed on a `phase/N-slug` branch in small Conventional Commits and merged into
`main` with a merge commit rather than a squash, so the phase boundaries and the work inside them
both stay visible. The pull request body for each phase is the phase write-up: what was built,
what was decided, what was deferred, how the definition of done was verified.

Commits that exist because of a specific scenario reference its id (`S01`–`S12`) in the body.
That trail is the evidence that the system was built against requirements rather than toward a
demo.

## Consequences

**Accepted costs.**
- Mixed-language tooling in one tree: `ruff` and `mypy` for Python, `biome` for TypeScript, two
  dependency managers, and CI jobs that must path-filter to stay fast.
- A merge-commit history is noisier than a squashed one and demands discipline in commit
  granularity, since the mess is permanent.

**Unlocked.**
- One clone, one bootstrap command, one compose stack for the demo.
- Shared domain types between the API and the console without publishing a package.
- A readable history that shows the order decisions were made in, which is the point.

**Revisit when.** The console needs an independent release cadence, or CI time on the full tree
exceeds the patience of the feedback loop.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Separate console repository | Two clones and two setup paths for a demo that has to work on someone else's machine in under ten minutes. |
| Squash-merge every phase | Produces a nine-commit log that demonstrates nothing about process. |
| Trunk-based with no phase branches | Loses the phase write-ups, which are among the more useful artifacts here. |
