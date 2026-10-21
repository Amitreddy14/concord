"""Tenant configuration.

Phase 2 needs only the tool scopes, because scope enforcement is one of this
phase's three acceptance criteria. The rest of the file — refund windows,
autonomy caps, risk tiers, disclosure levels — is loaded and exposed but not
yet interpreted; the policy engine that evaluates it arrives in phase 6.

Parsing the whole document now rather than only the part in use is deliberate.
A loader that silently ignores unknown keys would let a typo in a tenant's
autonomy cap sit undetected until phase 6, where it would look like a policy
bug rather than a configuration one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

TENANT_DIR = Path(__file__).resolve().parents[2] / "infra" / "tenants"


class TenantConfigError(ValueError):
    """Raised when a tenant file is missing or malformed."""


@dataclass(frozen=True, slots=True)
class RefundPolicy:
    window_days: int
    autonomy_cap_minor: dict[str, int]
    autonomous_reason_codes: frozenset[str]
    multi_capture_requires_approval: bool


@dataclass(frozen=True, slots=True)
class TenantConfig:
    tenant_id: str
    display_name: str
    default_currency: str
    refund_policy: RefundPolicy
    forbidden_actions: frozenset[str]
    tool_scopes: frozenset[str]
    grounding: dict[str, Any] = field(default_factory=dict)
    risk_tiers: list[dict[str, Any]] = field(default_factory=list)
    approvals: dict[str, Any] = field(default_factory=dict)
    disclosure: dict[str, Any] = field(default_factory=dict)
    sla: dict[str, Any] = field(default_factory=dict)
    reply_style: dict[str, Any] = field(default_factory=dict)

    def permits_scope(self, scope: str) -> bool:
        """Whether this deployment allows a tool to use this scope at all.

        The only input is configuration. Nothing derived from a ticket reaches
        this decision, which is the invariant that makes a successful prompt
        injection survivable: it may produce a bad draft, but it cannot widen
        what the agent is allowed to do.
        """
        return scope in self.tool_scopes

    @property
    def requires_citations(self) -> bool:
        return bool(self.grounding.get("require_citations", False))


def _require(document: dict[str, Any], key: str, source: Path) -> Any:
    if key not in document:
        raise TenantConfigError(f"{source.name}: missing required key {key!r}")
    return document[key]


def parse(document: dict[str, Any], source: Path) -> TenantConfig:
    refund = _require(document, "refund_policy", source)
    for key in ("window_days", "autonomy_cap_minor", "autonomous_reason_codes"):
        if key not in refund:
            raise TenantConfigError(f"{source.name}: refund_policy is missing {key!r}")

    caps = refund["autonomy_cap_minor"]
    for tier, value in caps.items():
        # Caps are money. A float here would be a float in an amount
        # comparison later, which the money rule forbids for good reason.
        if isinstance(value, bool) or not isinstance(value, int):
            raise TenantConfigError(
                f"{source.name}: autonomy cap for {tier!r} must be integer minor units, "
                f"got {type(value).__name__}"
            )

    scopes = _require(document, "tool_scopes", source)
    if not isinstance(scopes, list) or not scopes:
        raise TenantConfigError(f"{source.name}: tool_scopes must be a non-empty list")

    return TenantConfig(
        tenant_id=str(_require(document, "tenant_id", source)),
        display_name=str(document.get("display_name", document["tenant_id"])),
        default_currency=str(document.get("default_currency", "INR")),
        refund_policy=RefundPolicy(
            window_days=int(refund["window_days"]),
            autonomy_cap_minor=dict(caps),
            autonomous_reason_codes=frozenset(refund["autonomous_reason_codes"]),
            multi_capture_requires_approval=bool(
                refund.get("multi_capture_requires_approval", True)
            ),
        ),
        forbidden_actions=frozenset(document.get("forbidden_actions") or []),
        tool_scopes=frozenset(scopes),
        grounding=dict(document.get("grounding") or {}),
        risk_tiers=list(document.get("risk_tiers") or []),
        approvals=dict(document.get("approvals") or {}),
        disclosure=dict(document.get("disclosure") or {}),
        sla=dict(document.get("sla") or {}),
        reply_style=dict(document.get("reply_style") or {}),
    )


@lru_cache(maxsize=8)
def load_tenant(tenant_id: str, directory: Path | None = None) -> TenantConfig:
    """Load and cache one tenant's configuration.

    Cached because it is read on every scope check and the file does not
    change within a process. ``load_tenant.cache_clear()`` in tests.
    """
    directory = directory or TENANT_DIR
    source = directory / f"{tenant_id}.yaml"
    if not source.is_file():
        available = ", ".join(sorted(p.stem for p in directory.glob("*.yaml"))) or "none"
        raise TenantConfigError(f"no configuration for tenant {tenant_id!r}; available: {available}")
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TenantConfigError(f"{source.name}: not a mapping")
    return parse(document, source)
