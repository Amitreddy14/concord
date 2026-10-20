# tools

Contract-bound clients for the four systems of record, and the registry the agent calls them
through.

Every tool declares its name, argument schema, authorization scope, whether it mutates, and its
retry policy. Mutating tools carry a derived idempotency key — `hash(run_id, node, tool,
canonical_json(args))` — so a replayed graph step produces the same key and the system of record
returns the original response instead of acting twice.

Clients are generated against `contracts/`, never written against the mock implementations
(ADR 0002).
