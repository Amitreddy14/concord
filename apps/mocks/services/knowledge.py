"""Knowledge base.

Retrieval returns passages with stable ids, because a citation has to refer to
something durable: ids survive re-ingestion so stored citations and eval
fixtures do not rot.

Two behaviours are load-bearing for later phases. An empty result is a normal
outcome, not a failure to work around — it is the signal for the refusal path
in phase 4. And a passage outside its effective dates is never returned as
current policy, so a superseded refund window cannot be cited as if it still
applied.

The ranking here is lexical only. Phase 4 replaces it with hybrid dense and
lexical retrieval over pgvector; this exists so the services are complete and
the tool layer has something to integrate against, not as the real thing.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query

from apps.mocks.deps import get_store, page
from apps.mocks.state import Store
from packages.shared.problem import VALIDATION_FAILED, ProblemError

router = APIRouter(tags=["knowledge"])

WORD = re.compile(r"[a-z0-9]+")
MIN_TOP_K = 1
MAX_TOP_K = 20

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "what",
        "when",
        "with",
        "your",
    ]
)


def tokenize(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOPWORDS and len(w) > 1]


def is_effective(passage: dict[str, Any], on: date) -> bool:
    """Whether a passage states current policy on a given date."""
    start = passage.get("effective_from")
    end = passage.get("effective_to")
    if start and date.fromisoformat(str(start)) > on:
        return False
    return not (end and date.fromisoformat(str(end)) < on)


def score(query_terms: list[str], passage: dict[str, Any]) -> float:
    """Overlap between query and passage, normalised by query length.

    Deliberately crude. Making this cleverer would be building phase 4 early
    and in the wrong place; retrieval quality belongs behind the tool layer,
    not inside the mock.
    """
    if not query_terms:
        return 0.0
    haystack = " ".join(
        str(passage.get(field, "")) for field in ("title", "section", "text")
    ).lower()
    tokens = set(tokenize(haystack))
    hits = sum(1 for term in set(query_terms) if term in tokens)
    return hits / len(set(query_terms))


@router.post("/search")
async def search_passages(
    store: Annotated[Store, Depends(get_store)],
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ProblemError(VALIDATION_FAILED, "query is required")
    if not payload.get("tenant_id"):
        raise ProblemError(VALIDATION_FAILED, "tenant_id is required")

    top_k = int(payload.get("top_k", 6))
    if not MIN_TOP_K <= top_k <= MAX_TOP_K:
        raise ProblemError(VALIDATION_FAILED, f"top_k must be between {MIN_TOP_K} and {MAX_TOP_K}")
    min_score = payload.get("min_score")
    doc_types = set(payload.get("doc_types") or [])

    today = datetime.now(UTC).date()
    terms = tokenize(query)

    candidates: list[dict[str, Any]] = []
    for passage in store.passages.values():
        if doc_types and passage.get("doc_type") not in doc_types:
            continue
        if not is_effective(passage, today):
            continue
        value = score(terms, passage)
        if min_score is not None and value < float(min_score):
            continue
        if value <= 0:
            continue
        candidates.append({**passage, "score": round(value, 4)})

    # Ties break on passage id so repeated identical queries return an
    # identical order; replay depends on it.
    candidates.sort(key=lambda p: (-p["score"], p["passage_id"]))

    return {
        "passages": candidates[:top_k],
        "query_id": f"q_{abs(hash((query, tuple(sorted(doc_types)), top_k))) % 10**10:010d}",
    }


@router.get("/passages/{passage_id}")
async def get_passage(
    passage_id: str, store: Annotated[Store, Depends(get_store)]
) -> dict[str, Any]:
    passage = dict(store.passage(passage_id))
    passage.setdefault("score", 1.0)
    passage.setdefault("section", None)
    passage.setdefault("effective_from", None)
    passage.setdefault("effective_to", None)
    return passage


@router.get("/documents")
async def list_documents(
    store: Annotated[Store, Depends(get_store)],
    tenantId: Annotated[str, Query()],  # noqa: N803 - the contract names it this
    doc_type: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    documents = list(store.documents.values())
    if doc_type is not None:
        documents = [d for d in documents if d.get("doc_type") == doc_type]
    documents.sort(key=lambda d: d["document_id"])
    return page(documents)


@router.get("/incidents/active")
async def get_active_incidents(
    store: Annotated[Store, Depends(get_store)],
    tenantId: Annotated[str, Query()],  # noqa: N803 - the contract names it this
    affects: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    # Only unresolved incidents. A closed incident must not be offered to a
    # merchant as the explanation for a delay that is still happening (S07).
    incidents = [i for i in store.incidents.values() if i.get("status") != "resolved"]
    if affects is not None:
        incidents = [i for i in incidents if affects in (i.get("affects") or [])]
    incidents.sort(key=lambda i: i.get("started_at", ""), reverse=True)
    return {
        "incidents": [
            {**i, "expected_resolution_at": i.get("expected_resolution_at")} for i in incidents
        ]
    }
