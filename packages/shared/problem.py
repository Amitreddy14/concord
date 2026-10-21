"""RFC 9457 problem details — the only error shape any service returns.

One shape everywhere means the tool layer has exactly one thing to parse, and
``code`` gives it a stable key to map onto its own error taxonomy in Phase 7.
``retryable`` is advisory: the service knows whether *it* failed transiently,
but only the caller knows whether the operation is safe to repeat, so the tool
layer's taxonomy is authoritative and this field is a hint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROBLEM_CONTENT_TYPE = "application/problem+json"
BASE_TYPE = "https://concord.invalid/problems"


@dataclass(frozen=True, slots=True)
class ProblemCode:
    """A problem class: its URI suffix, HTTP status, title, and retryability."""

    code: str
    status: int
    title: str
    retryable: bool


# The closed set of problems these services emit. A handler that needs a
# problem not in this table is describing a failure mode nobody has thought
# about, which is worth a moment's thought rather than an ad-hoc string.
VALIDATION_FAILED = ProblemCode("validation_failed", 400, "Request is malformed", False)
NOT_FOUND = ProblemCode("not_found", 404, "Resource not found", False)
IDEMPOTENCY_KEY_REUSED = ProblemCode(
    "idempotency_key_reused", 409, "Idempotency key reused with different arguments", False
)
STATE_CONFLICT = ProblemCode("state_conflict", 409, "Conflicts with current state", False)
NOT_PERMISSIBLE = ProblemCode(
    "not_permissible", 422, "Not permissible against current state", False
)
ILLEGAL_TRANSITION = ProblemCode("illegal_transition", 422, "Illegal state transition", False)
CITATIONS_REQUIRED = ProblemCode("citations_required", 422, "Citations are required", False)
RATE_LIMITED = ProblemCode("rate_limited", 429, "Too many requests", True)
UPSTREAM_FAILURE = ProblemCode("upstream_failure", 500, "Upstream dependency failed", True)
UPSTREAM_TIMEOUT = ProblemCode("upstream_timeout", 504, "Upstream dependency timed out", True)


@dataclass(frozen=True, slots=True)
class Problem:
    """A problem detail ready to serialise as the body of an error response."""

    code: ProblemCode
    detail: str
    instance: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> int:
        return self.code.status

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": f"{BASE_TYPE}/{self.code.code}",
            "title": self.code.title,
            "status": self.code.status,
            "detail": self.detail,
            "code": self.code.code,
            "retryable": self.code.retryable,
        }
        if self.instance is not None:
            payload["instance"] = self.instance
        payload.update(self.extra)
        return payload


class ProblemError(Exception):
    """Raised inside a handler; converted to a problem response by the app."""

    def __init__(
        self,
        code: ProblemCode,
        detail: str,
        *,
        instance: str | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(detail)
        self.problem = Problem(code=code, detail=detail, instance=instance, extra=extra)


def not_found(what: str, identifier: str) -> ProblemError:
    return ProblemError(NOT_FOUND, f"no {what} with id {identifier}", instance=identifier)
