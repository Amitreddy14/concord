"""Knowledge base.

Retrieval returns passages with stable ids, because a citation has to refer to
something durable: ids survive re-ingestion so stored citations and eval
fixtures do not rot.

Two behaviours are load-bearing for later phases. An empty result is a normal
outcome, not a failure to work around — it is the signal for the refusal path
in phase 4. And a passage outside its effective dates is never returned as
current policy, so a superseded refund window cannot be cited as if it still
applied.

Retrieval is hybrid — BM25 and dense vectors fused by reciprocal rank — and
lives in ``packages.knowledge``, so the same engine serves this mock and a
real deployment. What the mock stands in for is the *corpus being someone
else's*, not the retrieval being fake.

An empty result is a normal outcome and the signal for the agent's refusal
path. A retriever that always returns something is a retriever that makes the
agent incapable of saying "the policy does not cover this".
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request

from apps.mocks.deps import get_store, page
from apps.mocks.state import Store
from packages.knowledge.corpus import CorpusError, build_index
from packages.knowledge.retrieval import HybridIndex, content_terms
from packages.shared.problem import UPSTREAM_FAILURE, VALIDATION_FAILED, ProblemError

router = APIRouter(tags=["knowledge"])

MIN_TOP_K = 1
MAX_TOP_K = 20


def is_effective(passage: dict[str, Any], on: date) -> bool:
    """Whether a passage states current policy on a given date."""
    start = passage.get("effective_from")
    end = passage.get("effective_to")
    if start and date.fromisoformat(str(start)) > on:
        return False
    return not (end and date.fromisoformat(str(end)) < on)


async def index_for(request: Request, tenant_id: str) -> HybridIndex | None:
    """The hybrid index for a tenant, if a corpus is configured for it.

    Seeded fixtures take precedence: a scenario that pins exact passages is
    describing a requirement, and silently serving it a different corpus would
    make the fixture a suggestion.
    """
    store: Store = request.app.state.store
    if store.passages:
        return None
    cached: dict[str, HybridIndex] = request.app.state.indexes
    if tenant_id not in cached:
        try:
            cached[tenant_id] = await build_index(tenant_id, cache=False)
        except CorpusError as error:
            # A tenant with no corpus configured at all is a legitimate state
            # during development; one whose configured corpus is missing is a
            # deployment error and must not be served as "no results".
            if "declares no grounding.corpus" in str(error):
                return None
            raise ProblemError(
                UPSTREAM_FAILURE, f"corpus unavailable for {tenant_id}: {error}"
            ) from error
    return cached[tenant_id]


@router.post("/search")
async def search_passages(
    request: Request,
    store: Annotated[Store, Depends(get_store)],
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ProblemError(VALIDATION_FAILED, "query is required")
    tenant_id = payload.get("tenant_id")
    if not tenant_id:
        raise ProblemError(VALIDATION_FAILED, "tenant_id is required")

    top_k = int(payload.get("top_k", 6))
    if not MIN_TOP_K <= top_k <= MAX_TOP_K:
        raise ProblemError(
            VALIDATION_FAILED, f"top_k must be between {MIN_TOP_K} and {MAX_TOP_K}"
        )
    min_score = payload.get("min_score")
    doc_types = set(payload.get("doc_types") or [])
    today = datetime.now(UTC).date()

    index = await index_for(request, str(tenant_id))
    if index is not None:
        hits = await index.search(
            query,
            top_k=top_k,
            doc_types=doc_types or None,
            min_score=float(min_score) if min_score is not None else None,
            on_date=today.isoformat(),
        )
        passages = [{**h.passage.to_payload(), "score": h.score} for h in hits]
    else:
        # Fixture-seeded passages: scored by term overlap, which is enough for
        # a fixture that pins which passages exist and expects them found.
        terms = set(content_terms(query))
        scored: list[dict[str, Any]] = []
        for passage in store.passages.values():
            if doc_types and passage.get("doc_type") not in doc_types:
                continue
            if not is_effective(passage, today):
                continue
            haystack = set(
                content_terms(
                    " ".join(
                        str(passage.get(f, "")) for f in ("title", "section", "text")
                    )
                )
            )
            overlap = len(terms & haystack) / len(terms) if terms else 0.0
            if overlap <= 0 or (min_score is not None and overlap < float(min_score)):
                continue
            scored.append({**passage, "score": round(overlap, 4)})
        scored.sort(key=lambda p: (-p["score"], p["passage_id"]))
        passages = scored[:top_k]

    return {
        "passages": passages,
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
