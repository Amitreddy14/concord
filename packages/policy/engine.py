"""The policy engine.

ADR 0004: the model never decides whether an action is permitted, never
computes an amount, and never assigns a risk tier. This is where those
decisions live, as pure functions over a tenant's configuration and facts read
from the systems of record.

Every decision carries a rule id and the inputs it was evaluated against. That
is not logging — it is the difference between a reviewer being able to answer
"why was this refunded?" and being handed a paraphrase generated after the
fact. A decision without a rule id is an opinion.

Three kinds of answer, and the distinction matters:

``permitted``
    The tenant's rules allow this, within the agent's autonomy.

``needs_approval``
    Allowed in principle, above the agent's autonomy. A human decides.

``forbidden``
    Not allowed by anyone through this system. No approval unlocks it, and
    offering one would imply a reviewer could authorise what the deployment
    has placed out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from packages.policy.tenant import TenantConfig
from packages.shared.money import Money


class Permission(StrEnum):
    PERMITTED = "permitted"
    NEEDS_APPROVAL = "needs_approval"
    FORBIDDEN = "forbidden"
    REFUSED = "refused"
    """Allowed in principle but not against these facts — outside the window,
    beyond the remainder. A human cannot wave it through because the obstacle
    is the state of the world, not the agent's authority."""


@dataclass(frozen=True, slots=True)
class ProposedAction:
    """What the agent wants to do, before anyone has said whether it may."""

    kind: str
    capture_id: str | None = None
    amount: Money | None = None
    reason_code: str | None = None
    captures_touched: tuple[str, ...] = ()
    touches: tuple[str, ...] = ()
    """Sensitive areas this action reaches: kyc, payout_hold, chargeback."""

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "capture_id": self.capture_id,
            "amount": self.amount.to_payload() if self.amount else None,
            "reason_code": self.reason_code,
            "captures_touched": list(self.captures_touched),
            "touches": list(self.touches),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProposedAction:
        """Rebuild a proposal that went through the graph's state.

        The round trip exists because LangGraph's state is JSON-shaped and a
        proposal has to survive a process restart while a reviewer decides.
        Amounts come back through ``Money.from_payload``, so a float that
        found its way into a checkpoint fails here rather than being refunded.
        """
        amount = payload.get("amount")
        return cls(
            kind=str(payload["kind"]),
            capture_id=payload.get("capture_id"),
            amount=Money.from_payload(amount) if amount else None,
            reason_code=payload.get("reason_code"),
            captures_touched=tuple(payload.get("captures_touched") or ()),
            touches=tuple(payload.get("touches") or ()),
        )


@dataclass(frozen=True, slots=True)
class Decision:
    permission: Permission
    rule_id: str
    risk_tier: str | None = None
    required_scope: str | None = None
    reasons: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    citations: list[str] = field(default_factory=list)
    """Passage ids a refusal can quote, so the merchant is told which rule."""

    expires_at: datetime | None = None

    @property
    def may_act_autonomously(self) -> bool:
        return self.permission is Permission.PERMITTED

    @property
    def needs_human(self) -> bool:
        return self.permission is Permission.NEEDS_APPROVAL

    def to_payload(self) -> dict[str, Any]:
        return {
            "permission": str(self.permission),
            "rule_id": self.rule_id,
            "risk_tier": self.risk_tier,
            "required_scope": self.required_scope,
            "reasons": self.reasons,
            # Recorded so a reviewer sees what the rule was evaluated against,
            # not only which rule fired.
            "inputs": self.inputs,
            "citations": self.citations,
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z")
            if self.expires_at
            else None,
        }


# Sensitive areas that no risk tier can unlock. Kept separate from the tier
# table because "high risk" and "out of scope" are different claims: a tier is
# a judgement about danger, this is a statement about authority.
# Actions that do not move money or change a system of record's authoritative
# state. A reply writes to the ticket, which is a record of the conversation
# rather than of the money, so it sits here. Defined once because `evaluate`
# dispatches on it and the `mutating:` tier condition is its negation —
# deriving that separately is how `reply` came to be mutating in one place and
# informational in the other.
NON_MUTATING_KINDS = frozenset({"read", "reply", "escalate", "note"})

ACTION_FOR_TOUCH = {
    "kyc": "change_kyc_status",
    "payout_hold": "lift_payout_hold",
    "chargeback": "accept_chargeback",
    "settlement_schedule": "alter_settlement_schedule",
    "merchant_tier": "modify_merchant_tier",
}

# The rule ids this engine can return. `W1-reason-code-not-autonomous` and
# `W2-multi-capture-requires-approval` used to sit between W0 and W3 and are
# deliberately gone: they are vestiges of the design where the engine decided
# approval itself, which is the defect Phase 6 fixed by making the tier table
# the single authority. A refund held for a non-autonomous reason now returns
# the tier that held it (`R2b-…`), not a parallel code for the same fact.
#
# Leaving them defined was an invitation to reintroduce two places deciding
# the same thing — the failure that produced a tier reading `approval: none`
# while the engine demanded one. `test_every_rule_id_is_reachable` keeps the
# vocabulary honest from here.
#
# The W-prefixed codes that remain are the ones no reviewer can overrule,
# because they describe the state of the world rather than the agent's
# authority.
RULE_FORBIDDEN = "F0-forbidden-action"
RULE_OUTSIDE_WINDOW = "W0-outside-refund-window"
RULE_EXCEEDS_REMAINDER = "W3-exceeds-unrefunded-remainder"
RULE_NO_TIER_MATCHED = "T0-no-tier-matched"


def evaluate(
    action: ProposedAction,
    *,
    tenant: TenantConfig,
    merchant: dict[str, Any],
    capture: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Decision:
    """Decide whether an action may proceed, and under whose authority."""
    now = now or datetime.now(UTC)
    inputs: dict[str, Any] = {
        "action": action.kind,
        "merchant_tier": merchant.get("tier"),
        "tenant": tenant.tenant_id,
    }

    # 1. Forbidden actions, before anything else. Checking these after a tier
    #    match would let a tier appear to authorise something no tier can —
    #    this tenant's table has an R4 tier routing chargeback work to a
    #    specialist queue, and `accept_chargeback` is also on the forbidden
    #    list. The list wins, by its own terms: "actions no risk tier can
    #    unlock".
    #
    #    The tier is still recorded, because the two fields answer different
    #    questions. `risk_tier` classifies what kind of action this is;
    #    `rule_id` names what decided it. A reviewer asking "why was this
    #    refused?" needs the rule, and one asking "what sort of request was
    #    it?" needs the tier, and collapsing them into one field loses
    #    whichever question is asked second.
    for touch in action.touches:
        forbidden_action = ACTION_FOR_TOUCH.get(touch)
        if forbidden_action and forbidden_action in tenant.forbidden_actions:
            classified = _match_risk_tier(
                tenant, action=action, within_cap=False, reason_autonomous=False
            )
            return Decision(
                Permission.FORBIDDEN,
                rule_id=RULE_FORBIDDEN,
                risk_tier=str(classified["id"]) if classified else None,
                reasons=[
                    f"{forbidden_action} is out of scope for this deployment; "
                    "no approval unlocks it"
                ],
                inputs={**inputs, "touch": touch, "forbidden_action": forbidden_action},
            )

    if action.kind != "create_refund":
        # Non-mutating and low-consequence actions take the informational
        # tier. Anything genuinely new arrives here and is refused rather than
        # waved through, so an unmodelled action cannot default to permitted.
        if action.kind in NON_MUTATING_KINDS:
            matched = _match_risk_tier(
                tenant, action=action, within_cap=True, reason_autonomous=True
            )
            tier_id = str(matched["id"]) if matched else RULE_NO_TIER_MATCHED
            return Decision(
                Permission.PERMITTED if matched else Permission.REFUSED,
                rule_id=tier_id,
                risk_tier=tier_id if matched else None,
                inputs=inputs,
            )
        return Decision(
            Permission.REFUSED,
            rule_id=RULE_NO_TIER_MATCHED,
            reasons=[f"no rule covers the action {action.kind!r}"],
            inputs=inputs,
        )

    return _evaluate_refund(
        action, tenant=tenant, merchant=merchant, capture=capture, now=now, inputs=inputs
    )


def _evaluate_refund(
    action: ProposedAction,
    *,
    tenant: TenantConfig,
    merchant: dict[str, Any],
    capture: dict[str, Any] | None,
    now: datetime,
    inputs: dict[str, Any],
) -> Decision:
    policy = tenant.refund_policy
    reasons: list[str] = []

    if action.amount is None or capture is None:
        return Decision(
            Permission.REFUSED,
            rule_id=RULE_NO_TIER_MATCHED,
            reasons=["a refund proposal must name an amount and a capture"],
            inputs=inputs,
        )

    # 2. The refund window. A refusal on these grounds is not an authority
    #    problem, so no approval resolves it: the capture is simply too old.
    captured_at = str(capture.get("captured_at", ""))
    age_days = _age_in_days(captured_at, now)
    inputs["capture_age_days"] = age_days
    inputs["window_days"] = policy.window_days

    if age_days is None:
        return Decision(
            Permission.REFUSED,
            rule_id=RULE_OUTSIDE_WINDOW,
            reasons=[f"capture {action.capture_id} has no readable capture date"],
            inputs=inputs,
        )
    if age_days > policy.window_days:
        return Decision(
            Permission.REFUSED,
            rule_id=RULE_OUTSIDE_WINDOW,
            reasons=[
                f"the capture is {age_days} days old and this tenant's refund window "
                f"is {policy.window_days} days"
            ],
            inputs=inputs,
            citations=["psg_refund-policy.v3.eligibility-window"],
        )

    # 3. The amount against what remains. Computed from the capture read from
    #    the ledger, never from anything the model produced.
    try:
        captured = Money.from_payload(capture["amount"])
        refunded = Money.from_payload(capture["refunded_amount"])
    except (KeyError, ValueError) as error:
        return Decision(
            Permission.REFUSED,
            rule_id=RULE_EXCEEDS_REMAINDER,
            reasons=[f"capture amounts could not be read: {error}"],
            inputs=inputs,
        )

    remaining = captured - refunded
    inputs["unrefunded_remainder"] = remaining.to_payload()
    inputs["requested"] = action.amount.to_payload()

    if action.amount > remaining:
        return Decision(
            Permission.REFUSED,
            rule_id=RULE_EXCEEDS_REMAINDER,
            reasons=[
                f"the proposed {action.amount.format()} exceeds the unrefunded remainder "
                f"{remaining.format()}"
            ],
            inputs=inputs,
        )

    # 4. From here the action is allowed in principle; what remains is whose
    #    authority it needs — and that is the tier table's answer, not a
    #    second computation here. Deciding it in both places is how a tier
    #    came to say `approval: none` while the engine demanded one.
    tier_name = str(merchant.get("tier", "standard"))
    cap_minor = policy.autonomy_cap_minor.get(tier_name)
    inputs["autonomy_cap"] = cap_minor

    within_cap = cap_minor is not None and action.amount.amount_minor <= cap_minor
    reason_autonomous = action.reason_code in policy.autonomous_reason_codes
    spans_multiple = policy.multi_capture_requires_approval and len(action.captures_touched) > 1
    inputs |= {
        "within_autonomy_cap": within_cap,
        "reason_in_autonomous_set": reason_autonomous,
        "spans_multiple_captures": spans_multiple,
    }

    matched = _match_risk_tier(
        tenant,
        action=action,
        within_cap=within_cap,
        reason_autonomous=reason_autonomous,
        spans_multiple=spans_multiple,
    )
    if matched is None:
        # No tier matched, which means the tenant's table has a gap. Refusing
        # is the only safe reading: defaulting to permitted would let a
        # configuration omission grant autonomy.
        return Decision(
            Permission.REFUSED,
            rule_id=RULE_NO_TIER_MATCHED,
            reasons=[
                "no risk tier in this tenant's configuration matches the proposal; "
                "the tier table has a gap"
            ],
            inputs=inputs,
        )

    tier_id = str(matched["id"])
    approval = str(matched.get("approval", "reviewer"))

    if not within_cap:
        cap_text = Money(cap_minor, action.amount.currency).format() if cap_minor else "none"
        reasons.append(
            f"{action.amount.format()} is above the {tier_name}-tier autonomy cap of {cap_text}"
        )
    if not reason_autonomous:
        reasons.append(
            f"reason code {action.reason_code!r} is outside this tenant's autonomous set"
        )
    if spans_multiple:
        reasons.append(
            f"the proposal spans {len(action.captures_touched)} captures, and this tenant "
            "requires a human for multi-capture refunds even when each leg is under the cap"
        )

    if approval == "forbidden":
        return Decision(
            Permission.FORBIDDEN,
            rule_id=tier_id,
            risk_tier=tier_id,
            reasons=reasons or [f"tier {tier_id} forbids this action"],
            inputs=inputs,
        )

    if approval == "none":
        return Decision(
            Permission.PERMITTED,
            rule_id=tier_id,
            risk_tier=tier_id,
            required_scope="payments:refund:write",
            reasons=["within the tier cap, with an autonomous reason code"],
            inputs=inputs,
        )

    hours = int(tenant.approvals.get("expires_after_hours", 48))
    return Decision(
        Permission.NEEDS_APPROVAL,
        rule_id=tier_id,
        risk_tier=tier_id,
        required_scope="payments:refund:write",
        reasons=reasons or [f"tier {tier_id} requires {approval}"],
        inputs=inputs,
        expires_at=now + timedelta(hours=hours),
    )


def _match_risk_tier(
    tenant: TenantConfig,
    *,
    action: ProposedAction,
    within_cap: bool,
    reason_autonomous: bool,
    spans_multiple: bool = False,
) -> dict[str, Any] | None:
    """The first tier in the tenant's ordered list whose conditions hold.

    Deliberately not a general expression language. The conditions a tenant
    can write are a fixed, named set, so a typo in a tenant file is a loud
    failure here rather than a silently false predicate that quietly grants
    autonomy.
    """
    for tier in tenant.risk_tiers:
        when = tier.get("when") or {}
        if _conditions_hold(
            when,
            action=action,
            within_cap=within_cap,
            reason_autonomous=reason_autonomous,
            spans_multiple=spans_multiple,
        ):
            return tier
    return None


KNOWN_CONDITIONS = {
    "mutating",
    "action",
    "within_autonomy_cap",
    "reason_in_autonomous_set",
    "spans_multiple_captures",
    "touches",
}


def _conditions_hold(
    when: dict[str, Any],
    *,
    action: ProposedAction,
    within_cap: bool,
    reason_autonomous: bool,
    spans_multiple: bool = False,
) -> bool:
    unknown = set(when) - KNOWN_CONDITIONS
    if unknown:
        raise ValueError(
            f"risk tier uses unknown condition(s) {sorted(unknown)}; "
            f"supported: {sorted(KNOWN_CONDITIONS)}"
        )

    mutating = action.kind not in NON_MUTATING_KINDS
    if "mutating" in when and when["mutating"] is not mutating:
        return False
    if "action" in when and when["action"] != action.kind:
        return False
    if "within_autonomy_cap" in when and when["within_autonomy_cap"] is not within_cap:
        return False
    if (
        "reason_in_autonomous_set" in when
        and when["reason_in_autonomous_set"] is not reason_autonomous
    ):
        return False
    if "spans_multiple_captures" in when and when["spans_multiple_captures"] is not spans_multiple:
        return False
    return not ("touches" in when and not set(when["touches"]) & set(action.touches))


def _age_in_days(captured_at: str, now: datetime) -> int | None:
    try:
        when = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((now - when).total_seconds()) // 86400
