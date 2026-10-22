# ADR 0008: The console holds no policy, and says so in code

- **Status:** accepted
- **Date:** 2026-10-22
- **Phase:** 6
- **Deciders:** Amit Reddy

## Context

The reviewer console has to stop a reviewer from submitting a decision the API will refuse. A
reviewer who types 50,000 into an amount field and learns after a round trip that a reviewer may
only reduce a proposal has been told late, and told badly.

So the console needs the rule. The question is what having it means, and the two readings produce
very different systems. If the console's copy of the rule is *a* check, the API's copy is still
the authority and the console is a faster way to learn the same answer. If it is *the* check, then
anyone with a browser console is the policy engine, and the rule that a refund cannot exceed the
unrefunded remainder is enforced by JavaScript running on the reviewer's laptop.

This is not hypothetical paranoia about malicious reviewers. The realistic failure is drift: the
tenant's `require_reviewer_note_on` list changes, the console is updated, the API is not, and
nobody notices because the console stopped sending the case that would have been refused. The
server's check is the one that keeps the system honest about its own configuration.

The same question applies to the evidence panel, from the other direction. ADR 0005 rule 4 holds
that the panel which authorises money carries no merchant-supplied text. The API does not send the
ticket body — the agent tests assert that. But "the API does not send it" and "the console cannot
render it" are different guarantees, and the first one is a guarantee about today's payload.

## Decision

Duplicate the rules into the console as user experience, and keep every one of them enforced on
the server. Nothing in `apps/console` is ever the reason a decision is accepted; it is only ever a
reason to stop and say so. Each duplicated rule names its server counterpart in a comment, so a
reader can find the authoritative copy, and the client-side module says in its own docstring that
it is not defence in depth.

For the evidence panel, validate the shape rather than trusting the sender: the console parses
responses through zod schemas that strip unknown keys, so a `body` field added to the payload
upstream cannot reach a component even by accident.

## Consequences

**Accepted costs.** Two copies of several rules, which can drift. The drift is bounded by what
each copy can do: the console drifting lax means the server refuses and the reviewer sees a problem
detail, and the console drifting strict means a reviewer is blocked from something legitimate and
complains. Neither produces a refund nobody authorised, which is the only failure that cannot be
walked back. The comments pointing at `apps/api/approvals.py` are the mitigation, and they are
worth less than a shared schema would be.

**Unlocked.** Validation as the reviewer types, without the server trusting any of it. Parsing at
the boundary also means a field the API renames surfaces as a parse failure rather than as
`undefined` rendered into the panel someone is deciding against.

**Revisit when.** The rule set grows past what a comment can keep in step — a second tenant with
materially different approval rules would be the trigger. At that point the honest move is to
serve the constraints from the tenant configuration as data the console renders, rather than
reimplementing them in TypeScript at all.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Validate only on the server | Correct and hostile: every mistake costs a round trip, and the reviewer learns the rule by hitting it. |
| Validate only in the console | Then the policy engine runs on the reviewer's laptop, and a configuration change that the server never learned about is invisible. |
| Generate the client checks from the Python | The right answer eventually, and overbuilt for four rules. It needs a schema export the API does not have, and the honest version of it is serving the constraints as data — which is what the revisit condition above describes. |
| Trust the API not to send the ticket body | True today, and a guarantee about today's payload rather than about the console. Stripping unknown keys costs nothing and holds whatever the payload grows into. |
