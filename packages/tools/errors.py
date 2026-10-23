"""The error taxonomy.

Every failure a tool can encounter is classified into one of four kinds, and
the kind — not the HTTP status — decides what happens next. Phase 7 builds
compensation and the dead-letter queue on this classification, which is why it
is defined here rather than improvised at each call site.

The distinction that matters most is ``RETRYABLE`` versus ``INDETERMINATE``. A
connection error before the request was sent is safe to retry freely. A
timeout *after* the request was accepted is not safe in the same way: the
effect may have happened, and only the idempotency key makes the retry sound.
Collapsing the two would make the tool layer's behaviour depend on luck.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorKind(StrEnum):
    RETRYABLE = "retryable"
    """Transient and safe to repeat: 429, 5xx, connection refused."""

    INDETERMINATE = "indeterminate"
    """The request may or may not have taken effect — a timeout after send.

    Retryable only because the key makes it so. Phase 7's reconciliation pass
    treats these as the cases worth re-checking against the system of record.
    """

    FATAL = "fatal"
    """The request is wrong and repeating it will not help: 400, 404, 409."""

    NEEDS_HUMAN = "needs_human"
    """Refused on policy or state grounds; a person has to decide. 422."""

    POISONED_INPUT = "poisoned_input"
    """The arguments failed validation before any call was made.

    Separated from FATAL because it never reached the network, so there is
    nothing to reconcile and nothing to compensate.
    """


# HTTP status to kind, for problems the service returned.
_BY_STATUS: dict[int, ErrorKind] = {
    400: ErrorKind.FATAL,
    401: ErrorKind.FATAL,
    403: ErrorKind.FATAL,
    404: ErrorKind.FATAL,
    # The server gave up waiting for the request. It is unlikely to have
    # applied anything, and "unlikely" is not "did not" — for a mutation the
    # difference is a second refund, so it is indeterminate rather than
    # retryable.
    408: ErrorKind.INDETERMINATE,
    409: ErrorKind.FATAL,
    422: ErrorKind.NEEDS_HUMAN,
    429: ErrorKind.RETRYABLE,
    500: ErrorKind.RETRYABLE,
    502: ErrorKind.RETRYABLE,
    503: ErrorKind.RETRYABLE,
    504: ErrorKind.INDETERMINATE,
}

# Problem codes that override the status mapping. The service knows things the
# status alone does not — notably that a timeout happened after the effect was
# applied, which is the whole of S09.
#
# `state_conflict` used to be here as RETRYABLE, and it was wrong twice over.
# It contradicted `problem.py`, which declares the same code `retryable:
# False`, and it was wrong on its own terms: a refund that conflicts because
# the capture is already fully refunded conflicts identically on every
# attempt, so retrying spends the budget to arrive at the same answer. It is
# FATAL by status now, with no override. The consistency test below is what
# caught the disagreement and what keeps the two tables in step.
_BY_CODE: dict[str, ErrorKind] = {
    "upstream_timeout": ErrorKind.INDETERMINATE,
    "idempotency_key_reused": ErrorKind.FATAL,
    "not_permissible": ErrorKind.NEEDS_HUMAN,
    "illegal_transition": ErrorKind.FATAL,
    "citations_required": ErrorKind.NEEDS_HUMAN,
}


#: The 5xx range, named because the two status classes mean opposite things
#: here and a bare 500 in a comparison does not say which boundary it is.
SERVER_ERROR_FLOOR = 500
SERVER_ERROR_CEILING = 600


def _by_status_class(status: int | None) -> ErrorKind:
    """The default for a status nothing maps explicitly.

    By class rather than by a single fallback, because the two classes mean
    opposite things. A 5xx is the server's problem and is worth another
    attempt; a 4xx is the request's problem and repeating it changes nothing.

    The previous `or ErrorKind.FATAL` fallback got that backwards for every
    unlisted 5xx: a 507 or a 521 was classified as fatal and never retried,
    which is the one case where a retry would almost certainly have worked.
    """
    if status is None:
        return ErrorKind.FATAL
    if SERVER_ERROR_FLOOR <= status < SERVER_ERROR_CEILING:
        return ErrorKind.RETRYABLE
    return ErrorKind.FATAL


@dataclass(frozen=True, slots=True)
class ToolFailure:
    """A classified failure, carrying enough to decide and to audit."""

    kind: ErrorKind
    code: str
    status: int | None
    detail: str
    tool: str
    attempt: int = 1
    retry_after_s: int | None = None
    effect_applied: bool | None = None
    """Whether the service said the effect was applied despite the failure.

    Only ever set from the service's own statement. Absent means unknown, and
    unknown is not the same as no.
    """

    @property
    def retryable(self) -> bool:
        return self.kind in (ErrorKind.RETRYABLE, ErrorKind.INDETERMINATE)


class ToolError(Exception):
    """Raised by the invoker when a tool call fails terminally."""

    def __init__(self, failure: ToolFailure) -> None:
        super().__init__(f"{failure.tool}: {failure.code} — {failure.detail}")
        self.failure = failure


class ScopeViolation(Exception):
    """A tool was invoked with a scope this tenant does not permit.

    Raised before any network call. This is not an error class a retry can
    help with, and it is deliberately not a ToolError: a scope violation means
    something tried to do what the deployment forbids, and that is a different
    conversation from a dependency being slow.
    """

    def __init__(self, tool: str, scope: str, tenant: str) -> None:
        super().__init__(
            f"tool {tool!r} requires scope {scope!r}, which tenant {tenant!r} does not permit"
        )
        self.tool = tool
        self.scope = scope
        self.tenant = tenant


def classify(
    *,
    tool: str,
    status: int | None,
    problem: dict[str, Any] | None,
    attempt: int = 1,
    transport_error: str | None = None,
) -> ToolFailure:
    """Turn a response or a transport failure into a classified failure."""
    if transport_error is not None:
        # Nothing came back. Whether the request was received is unknowable
        # from here, so the honest classification is indeterminate rather than
        # an optimistic retryable.
        return ToolFailure(
            kind=ErrorKind.INDETERMINATE,
            code="transport_error",
            status=None,
            detail=transport_error,
            tool=tool,
            attempt=attempt,
        )

    problem = problem or {}
    code = str(problem.get("code") or f"http_{status}")
    kind = _BY_CODE.get(code) or _BY_STATUS.get(status or 0) or _by_status_class(status)

    return ToolFailure(
        kind=kind,
        code=code,
        status=status,
        detail=str(problem.get("detail") or problem.get("title") or "no detail"),
        tool=tool,
        attempt=attempt,
        retry_after_s=problem.get("retry_after"),
        effect_applied=problem.get("effect_applied"),
    )
