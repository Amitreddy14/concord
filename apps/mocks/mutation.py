"""One implementation of the idempotent-mutation protocol.

Every mutating endpoint across the four services follows the same sequence:
reserve the key, perform the effect, record the response — and release the
reservation if the effect raised, so a legitimate retry is not blocked by a
handler that did nothing.

Written once rather than six times. The tool layer is built against all four
services and relies on them behaving identically here; six hand-rolled copies
would differ eventually, and the difference would surface as a double refund
rather than as a failing test.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Response

from apps.mocks.idempotency import IdempotencyStore

REPLAYED_HEADER = "Idempotency-Replayed"


def idempotent(
    *,
    idempotency: IdempotencyStore,
    key: str,
    method: str,
    path: str,
    payload: Any,
    response: Response,
    perform: Callable[[], tuple[int, dict[str, Any]]],
) -> dict[str, Any]:
    """Run ``perform`` at most once for ``key``.

    ``perform`` returns ``(status_code, body)`` and is called only when the key
    has not been seen. On replay the original body is returned with
    ``Idempotency-Replayed: true`` and the original status, and ``perform`` is
    never invoked — which is what makes a retry after a client timeout safe.
    """
    replayed = idempotency.reserve(key, method, path, payload)
    if replayed is not None:
        response.headers[REPLAYED_HEADER] = "true"
        if replayed.status_code is not None:
            response.status_code = replayed.status_code
        return replayed.response or {}

    try:
        status_code, body = perform()
    except BaseException:
        # The effect did not happen, so holding the key would wrongly block a
        # retry that should be allowed to proceed.
        idempotency.release(key)
        raise

    idempotency.complete(key, status_code, body)
    response.status_code = status_code
    return body
