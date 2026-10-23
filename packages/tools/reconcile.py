"""Finding out what actually happened, and what can be done about it.

An indeterminate failure leaves a question nothing in the run can answer from
memory: did the effect land? A timeout after the request was accepted looks
exactly like a timeout before it was received, and the difference is a refund.

Two things live here, and they are different jobs.

**Reconciliation** answers the question by reading the system of record. It
never guesses and never infers from the failure itself. Its three outcomes are
``LANDED``, ``ABSENT`` and ``UNKNOWN``, and the third is load-bearing: when the
read itself fails, the honest answer is that we still do not know. Collapsing
``UNKNOWN`` into ``ABSENT`` — treating "I could not check" as "it did not
happen" — is the single most expensive mistake available here, because the
next step after ABSENT is to try again.

**Compensation** is what to do when something happened that should not have,
and the uncomfortable finding is that for the action that matters it cannot be
done. The contract has six mutating tools and not one inverse: no
``cancel_refund``, no ``delete_note``. So compensation is not one mechanism but
three, chosen per tool, and for money it is not an undo at all:

``RESTORE``
    The tool sets a value, so the compensation is setting it back. Ticket
    status, assignment and flags work this way, and the prior value has to
    have been captured before the change or there is nothing to restore to.

``CORRECT``
    The record is append-only, so the compensation is appending a correction.
    A merchant note cannot be unwritten; a second note saying the first was
    wrong is the honest repair, and it leaves both visible.

``NONE``
    No inverse exists and inventing one would be worse than admitting it. A
    refund that has settled cannot be pulled back through this API. The
    strategy for money is therefore prevention (the approval gate), detection
    (this module) and escalation (the dead-letter queue) — and saying so is
    more use to an operator than a ``compensate()`` that quietly fails.

Phase 6's gate is what makes NONE tolerable: a refund above the cap never
happens without a human, so the unrecoverable case is narrow by construction
rather than by luck.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from packages.tools.errors import ErrorKind, ToolError, ToolFailure
from packages.tools.keys import derive_idempotency_key


class Resolution(StrEnum):
    LANDED = "landed"
    """The system of record holds the effect. It happened."""

    ABSENT = "absent"
    """The system of record was read successfully and does not hold it."""

    UNKNOWN = "unknown"
    """The read failed, so the question is still open.

    Never treated as ABSENT. The step after ABSENT is to try again, and trying
    again on an effect that did land is the duplicate this whole module exists
    to prevent.
    """


class Compensation(StrEnum):
    RESTORE = "restore"
    CORRECT = "correct"
    NONE = "none"


#: How each mutating tool can be compensated, and nothing is left to a
#: default. A tool absent from this table raises rather than silently
#: becoming NONE, because "nobody decided" and "no inverse exists" are
#: different facts and only one of them is acceptable.
COMPENSATION_FOR_TOOL: dict[str, Compensation] = {
    "create_refund": Compensation.NONE,
    "add_merchant_note": Compensation.CORRECT,
    "post_message": Compensation.CORRECT,
    "set_ticket_status": Compensation.RESTORE,
    "assign_ticket": Compensation.RESTORE,
    "flag_ticket": Compensation.RESTORE,
}


@dataclass(frozen=True, slots=True)
class Reconciliation:
    resolution: Resolution
    tool: str
    idempotency_key: str
    effect: dict[str, Any] | None = None
    """The record found upstream, when the resolution is LANDED."""
    detail: str = ""
    read_failure: ToolFailure | None = None
    checked: list[str] = field(default_factory=list)
    """Which reads were performed, for the audit trail."""

    @property
    def is_settled(self) -> bool:
        return self.resolution is not Resolution.UNKNOWN

    @property
    def compensation(self) -> Compensation:
        try:
            return COMPENSATION_FOR_TOOL[self.tool]
        except KeyError as error:
            raise ValueError(
                f"no compensation strategy recorded for the mutating tool {self.tool!r}; "
                "add one to COMPENSATION_FOR_TOOL rather than letting it default"
            ) from error

    def to_payload(self) -> dict[str, Any]:
        return {
            "resolution": str(self.resolution),
            "tool": self.tool,
            "idempotency_key": self.idempotency_key,
            "effect_id": (self.effect or {}).get("refund_id"),
            "compensation": str(self.compensation) if self.is_settled else None,
            "detail": self.detail,
            "checked": list(self.checked),
        }


async def reconcile_refund(
    invoker: Any,
    *,
    run_id: str,
    node: str,
    arguments: dict[str, Any],
    capture_id: str,
) -> Reconciliation:
    """Did this refund land?

    Answered by matching on the idempotency key rather than on the amount.
    The key is derived from the run, the node, the tool and the canonical
    arguments, so it identifies *this* attempt and nothing else — whereas a
    match on `(capture_id, amount)` would happily adopt a refund some other
    run issued for the same figure, which is a worse error than the one being
    fixed.

    Read-only. Re-issuing the mutation would also answer the question, via the
    idempotency replay, but it answers by performing the effect when the
    answer is "it did not happen" — and reconciliation runs precisely when
    nobody has decided that should happen again.
    """
    key = derive_idempotency_key(
        run_id=run_id, node=node, tool="create_refund", arguments=arguments
    )
    checked: list[str] = []

    try:
        capture = await invoker.call(
            "get_capture",
            {"capture_id": capture_id},
            run_id=run_id,
            node="reconcile",
        )
        checked.append("get_capture")
        payment_id = str(capture.data["payment_id"])

        payment = await invoker.call(
            "get_payment",
            {"payment_id": payment_id},
            run_id=run_id,
            node="reconcile",
        )
        checked.append("get_payment")
    except ToolError as error:
        # The read failed. We are no wiser than before, and saying so is the
        # only safe answer: an operator can retry a reconciliation, but
        # nobody can un-issue a second refund.
        return Reconciliation(
            resolution=Resolution.UNKNOWN,
            tool="create_refund",
            idempotency_key=key,
            detail=f"could not read the system of record: {error.failure.detail}",
            read_failure=error.failure,
            checked=checked,
        )

    for refund in payment.data.get("refunds") or []:
        if refund.get("idempotency_key") == key:
            return Reconciliation(
                resolution=Resolution.LANDED,
                tool="create_refund",
                idempotency_key=key,
                effect=refund,
                detail=f"refund {refund.get('refund_id')} exists upstream for this key",
                checked=checked,
            )

    return Reconciliation(
        resolution=Resolution.ABSENT,
        tool="create_refund",
        idempotency_key=key,
        detail="no refund upstream carries this key; the effect did not land",
        checked=checked,
    )


def needs_reconciliation(failure: ToolFailure, *, mutating: bool) -> bool:
    """Whether this failure leaves a question worth a read.

    Only mutations, and only when the outcome is genuinely unknown. A FATAL
    failure is the dependency telling us something true and a POISONED_INPUT
    never reached the network, so neither leaves anything upstream to find.
    """
    if not mutating:
        return False
    if failure.effect_applied is False:
        # The service stated it did not apply. Taking it at its word is
        # reasonable; it knows and we do not.
        return False
    return failure.kind is ErrorKind.INDETERMINATE
