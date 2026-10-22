"""What every node needs, in its own module.

Separate from ``nodes.py`` because both the core nodes and the specialists
depend on it, and the specialists are imported by ``nodes.py`` for the facts
renderer. Leaving it in ``nodes.py`` forced the specialists to type their
context as ``Any`` to avoid the cycle, which is how a type checker stops
catching anything about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from packages.policy.approval import ApprovalStore, InMemoryApprovalStore
from packages.policy.tenant import TenantConfig, load_tenant
from packages.shared.llm import LLMGateway
from packages.tools.invoker import ToolInvoker


@dataclass
class NodeContext:
    gateway: LLMGateway
    invoker: ToolInvoker
    tenant_id: str
    require_citations: bool = True
    min_passage_score: float | None = None

    tenant: TenantConfig = field(init=False)
    """The loaded configuration, not just its id. Phase 6's nodes evaluate
    policy against it, and passing the id and reloading inside each node
    would let two nodes in one run read two different versions of a tenant
    file edited mid-run."""

    approvals: ApprovalStore = field(default_factory=InMemoryApprovalStore)
    """Where pending approvals are indexed. Defaults to in-memory so a test
    needs no database; a deployment passes the Postgres-backed store."""

    def __post_init__(self) -> None:
        self.tenant = load_tenant(self.tenant_id)
