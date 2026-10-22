"""What every node needs, in its own module.

Separate from ``nodes.py`` because both the core nodes and the specialists
depend on it, and the specialists are imported by ``nodes.py`` for the facts
renderer. Leaving it in ``nodes.py`` forced the specialists to type their
context as ``Any`` to avoid the cycle, which is how a type checker stops
catching anything about it.
"""

from __future__ import annotations

from dataclasses import dataclass

from packages.shared.llm import LLMGateway
from packages.tools.invoker import ToolInvoker


@dataclass
class NodeContext:
    gateway: LLMGateway
    invoker: ToolInvoker
    tenant_id: str
    require_citations: bool = True
    min_passage_score: float | None = None
