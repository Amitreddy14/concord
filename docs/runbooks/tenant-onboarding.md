# Runbook: onboarding a tenant

Adding a customer is adding two things: a configuration file and a corpus. There is no code
change, and a test enforces that — `TestNoTenantSpecificBranch` walks the AST of every module in
`packages/` and fails if a tenant id, display name or sign-off appears as code.

`infra/tenants/northwind-pay.yaml` is the worked example. It was written to disagree with
`acme-payments` wherever a policy value can differ, so reading the two side by side shows which
knobs exist.

Budget about two hours for a tenant whose policy is already decided, and most of that is the
corpus. The configuration itself is twenty minutes.

---

## 1. Write the tenant file

Copy an existing file rather than starting from the schema. `infra/tenants/<tenant-id>.yaml`,
where the id is the one the ticketing system will send in its payloads — this is the only name
that has to match something outside the repo.

The loader parses the whole document, not only the parts currently read, so a typo in a key fails
at load rather than at the first run that needed it. Check it before going further:

```bash
.venv/bin/python -c "from packages.policy.tenant import load_tenant; print(load_tenant('<tenant-id>'))"
```

### The decisions that actually matter

Four blocks change what the agent is allowed to do. The rest change what it says.

**`refund_policy`.** The window, the per-tier autonomy caps in minor units, and the reason codes
that may go out without a human. Caps are minor units in the tenant's own currency: `25000` is
€250.00 for a EUR tenant and ₹250.00 for an INR one, and getting this wrong is the single
most expensive error available in this file.

**`risk_tiers`.** Ordered; first match wins; the matched tier's `approval` is what decides.
The engine computes the conditions and reads the answer here — it does not decide separately.
Two places deciding the same thing is the defect Phase 6 fixed, and the tier table is the
authority that fix established.

Order matters. Put compliance and dispute tiers above the refund tiers, or a refund that touches
a payout hold can match a permissive tier first.

A tenant that wants a human on every refund says so with `approval: reviewer` on its catch-all
refund tier. It does **not** say so by deleting the tier. A table with a gap makes the engine
refuse — correct, because a missing tier must not grant autonomy — but it refuses with
`T0-no-tier-matched`, which tells a reviewer the configuration is broken rather than that a human
is required. Northwind's `R1-reviewed-refund` is the right shape for this.

**`forbidden_actions`.** Actions no tier can unlock. These are not "high risk", they are out of
scope for the system. Note that `accept_chargeback` being here means the dispute tier classifies
chargeback work and never authorises it; the list wins by its own terms.

**`tool_scopes`.** The complete set this deployment enables. Nothing derived from a ticket can
widen it. Enable only what the tenant's desk actually does — Northwind omits
`payments:chargeback:read` because its disputes team is separate, and routing a ticket to that
queue is not the same as being able to read the dispute.

### The blocks that shape the reply

`disclosure` controls how much internal detail may be quoted — a payout hold reason as a category,
or not at all. `reply_style` sets tone, length, sign-off, and the commitments the agent may never
author. `grounding.min_passage_score` sets how good a passage match has to be before it can
support a claim; raising it turns weak matches into refusals.

---

## 2. Build the corpus

`grounding.corpus` lists a `doc_type` and a `source` directory per entry. The paths are read from
this file rather than from a convention mapping tenant id to directory name, so a tenant's corpus
can live wherever it likes.

Two tests cover this: every declared directory must exist, and must hold at least one document.
Both exist because the paths used to be strings the loader kept and never resolved — a tenant
could declare a corpus that was not there and the symptom was every query refusing, with nothing
to say why.

Documents are markdown with front matter: `document_id`, `doc_type`, `title`, `version`,
`effective_from`. Write them as the tenant's own policy rather than as a paraphrase of another
tenant's, because the agent quotes them to merchants and a passage that says "180 days" when the
configuration says 90 will be cited against the configuration.

The corpus must answer the questions the desk actually receives. A tenant whose refund policy
requires review for everything except duplicates needs a passage saying so, or the agent cannot
explain its own behaviour to a merchant and will refuse instead.

---

## 3. Record the tenant's expectations

Add cases to `evals/golden/conformance.yaml`. This is the step most likely to be skipped and the
one that catches a misread configuration.

Write the expected decision for each case by reading the tenant file and reasoning it out. Do
**not** run the engine and record what it says. A conformance suite recorded from the thing it
checks agrees with that thing by construction, including when that thing is wrong. The point of
the file is to be a second opinion.

Where the new tenant decides differently from an existing one, tag the case with the
configured dimension that causes it and add that dimension to `divergence_dimensions`. A
dimension that changes no outcome anywhere fails `test_every_dimension_actually_diverges`, which
means either the dimension does not matter or the suite is not exercising it — both worth knowing.

Keep some cases where the tenants agree. A suite in which they differ on everything is consistent
with an engine that reads the tenant id and nothing else.

```bash
make check
```

---

## 4. Walk it

```bash
make demo
```

The walkthrough runs against the deterministic stand-in, so it needs no GPU. `MODEL=qwen3:8b make
demo` substitutes a real model and nothing else.

---

## What this runbook does not cover

**A second tenant in one process.** Tenant configuration is cached per id and the graph takes a
tenant at construction, so serving two tenants concurrently is a deployment question — one process
per tenant, or a factory per request — and it has not been built or measured.

**Per-tenant business timezones.** `packages/shared/dates` renders in UTC deliberately: a
settlement is booked on the platform's business day, and rendering in the reader's zone would put
one settlement on two dates for two merchants. A tenant whose business day genuinely is not UTC
needs that threaded through as a tenant setting, which is a code change and therefore outside the
claim this runbook makes.

**Migrating an existing tenant's policy.** Changing a cap or a window on a live tenant affects
runs already paused for approval, whose proposals were priced under the old policy. Nothing
re-prices a pending approval, and nothing warns that one is stale.
