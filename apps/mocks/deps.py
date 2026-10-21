"""Request-scoped helpers shared by the service routers."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from apps.mocks.idempotency import IdempotencyStore
from apps.mocks.state import Store


def get_store(request: Request) -> Store:
    store: Store = request.app.state.store
    return store


def get_idempotency(request: Request) -> IdempotencyStore:
    store: IdempotencyStore = request.app.state.idempotency
    return store


def page(items: list[dict[str, Any]], limit: int | None = None) -> dict[str, Any]:
    """Wrap a list in the contract's Page shape.

    Cursor pagination is declared by the contract but not implemented here:
    no fixture approaches a page boundary, and a cursor nobody exercises is
    untested code pretending to be a feature.
    """
    if limit is not None and len(items) > limit:
        return {"items": items[:limit], "has_more": True, "next_cursor": None}
    return {"items": items, "has_more": False, "next_cursor": None}
