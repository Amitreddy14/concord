# Reviewer console

The surface a human uses to decide the refunds the agent cannot authorise alone: the approval
queue, the evidence behind each proposal, and the trace of what the run actually did.

It is a client of the API and holds no policy of its own. Every rule it appears to enforce — the
note a rejection requires, the amount a reviewer may not raise — is enforced in
`apps/api/approvals.py`, and that is the one with authority. The checks here exist so a reviewer
is told as they type rather than after a round trip. If the two ever disagree, the server refuses
and the console shows the problem detail.

---

## Running it

```bash
make console-install     # once
make api                 # the API this reads from, on :8000
make console-dev         # the console on :3000
```

`NEXT_PUBLIC_API_BASE_URL` points at the API and defaults to `http://localhost:8000`.

---

## What each screen is for

| Route | What it answers |
| --- | --- |
| `/` | What is waiting on a human, oldest first, and what is about to expire |
| `/approvals/[id]` | What the agent proposed, which rule sent it here, and the ledger facts behind it |
| `/runs/[runId]` | What the run actually did: nodes, tool calls, scopes, attempts, outcomes |

The queue is the landing page rather than a dashboard. A reviewer opening this wants the work;
a count of pending approvals is something they can see by looking at the list.

---

## Decisions worth knowing about

**Money is never divided.** `formatMoney` splits the integer at the currency's exponent and
`parseMoneyInput` reads digits. Neither multiplies or divides, so a reviewer's `20600.01` cannot
arrive as `2060000.9999999998`. The repository's no-floats-near-money rule reaches this directory
through `scripts/check_money_ts.py`, and `src/lib/money.ts` carries the unit tests that cover what
a line-based checker cannot see. Zero-decimal currencies are handled, because a ¥1,000 amount
stored as 1000 minor units is ¥1,000 and not ¥10.00.

**The evidence panel carries no merchant text.** ADR 0005 rule 4: the panel that authorises money
shows ledger facts and passage ids, never the ticket body. An injected instruction that reaches a
reviewer has found a human to carry it. `evidenceSchema` strips unknown keys, so a `body` field
added upstream cannot reach a component even by accident — there is a test asserting exactly that.

**Responses are parsed, not cast.** Zod at the boundary, for the same reason the Python side uses
Pydantic v2 for every boundary type: a type assertion is a promise about a payload nobody checked.
A renamed field surfaces as a parse failure rather than as `undefined` rendered into an evidence
panel.

**Status codes mean different things to do.** 404 means the queue is stale, 409 means someone else
decided it or the sweep expired it — reload, never retry — and 400 means the submission was
malformed, so the form stays populated and shows the detail. A console that retried a 400 would be
repeating its own mistake.

**Fonts are vendored through npm.** `next/font/google` reaches fonts.googleapis.com at build time,
and a build that needs a third party's CDN is one that fails in CI, on a locked-down network, and
on a plane — with the failure surfacing in the font layer, which is the last place anyone looks.
CI greps for its reintroduction.

**Monospace means machine identity.** Ids, amounts and rule ids are set in IBM Plex Mono because
they get copied into other systems and read down a column of figures. Prose is never monospace
here.

---

## Checks

```bash
npm --prefix apps/console run check    # biome, tsc --noEmit, vitest
```

`make check` at the repository root includes this, and skips it with a note when
`node_modules` is absent rather than failing on a missing install.
