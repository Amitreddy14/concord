"""Derived idempotency keys.

The domain model specifies the derivation:

    key = hash(run_id, node_name, tool_name, canonical_json(arguments))

Derived rather than generated, and that word does the work. A generated key is
new on every attempt, so a retry after a client timeout creates a second
effect — the S09 defect. A derived key is a function of what is being asked
for, so a replayed graph step produces the same key and the system of record
returns the original response.

The node name is in the hash on purpose. Two different nodes refunding the
same capture in one run is a bug, but it is a *different* bug from a retry,
and collapsing them under one key would hide it behind a silent replay rather
than surfacing it as a duplicate effect the ledger can show.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Long enough that collisions are not a consideration, short enough to read in
# a ledger row. The contract allows 16 to 128 characters.
KEY_HEX_LENGTH = 32


def canonical(arguments: Any) -> str:
    """Canonical JSON: sorted keys, no incidental whitespace.

    A body differing only in key order is the same request, and must produce
    the same key — otherwise a client library that happens to reorder a dict
    would silently defeat idempotency.
    """
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)


def derive_idempotency_key(*, run_id: str, node: str, tool: str, arguments: Any) -> str:
    """The key for one logical mutation within one run."""
    if not run_id or not node or not tool:
        raise ValueError("run_id, node and tool are all required to derive a key")
    material = f"{run_id}\x1f{node}\x1f{tool}\x1f{canonical(arguments)}"
    body = hashlib.blake2b(material.encode(), digest_size=KEY_HEX_LENGTH // 2).hexdigest()
    # Prefixed so a key found in a log or a ledger row is self-describing.
    return f"ck_{body}"
