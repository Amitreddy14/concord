# policy

The policy engine, risk tiering and tenant configuration loading.

Everything here is a pure function over a tenant config and a set of facts, with unit tests and
no model call anywhere in the path. The model proposes a typed action; this package decides
whether it is permitted, what risk tier it carries, and whether a human must approve it
(ADR 0004).

Every decision returns a rule id, which is written to the run ledger so a reviewer sees which
rule fired and on what inputs.
