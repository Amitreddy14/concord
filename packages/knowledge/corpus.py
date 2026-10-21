"""Loading a tenant's corpus and building its index.

The source paths come from the tenant's own ``grounding.corpus`` block rather
than from a convention mapping tenant id to directory name. The configuration
already declared them and nothing read it, which is how the directory came to
be named ``acme`` while the tenant is ``acme-payments`` — a mismatch that
surfaced as every query refusing, with no error to explain it.

Configuration that nothing reads is configuration that drifts. Reading it also
means a second tenant can keep its corpus wherever it likes, which is the
claim phase 9 has to make good on.

Indexes are cached per tenant: chunking and embedding takes real time with a
real embedder, and a corpus does not change within a process.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from packages.knowledge.chunking import Passage, chunk_corpus, chunk_document
from packages.knowledge.embeddings import Embedder, HashingEmbedder
from packages.knowledge.retrieval import HybridIndex
from packages.policy.tenant import TenantConfigError, load_tenant

REPO_ROOT = Path(__file__).resolve().parents[2]

_INDEXES: dict[str, HybridIndex] = {}


class CorpusError(ValueError):
    """A tenant's configured corpus is missing or empty."""


@dataclass(frozen=True, slots=True)
class CorpusSource:
    doc_type: str
    path: Path


def sources_for(tenant_id: str, root: Path | None = None) -> list[CorpusSource]:
    """The corpus directories a tenant declares, resolved to real paths."""
    try:
        tenant = load_tenant(tenant_id)
    except TenantConfigError as error:
        raise CorpusError(str(error)) from error

    base = root or REPO_ROOT
    declared = tenant.grounding.get("corpus") or []
    if not declared:
        raise CorpusError(f"tenant {tenant_id!r} declares no grounding.corpus sources")

    sources: list[CorpusSource] = []
    for entry in declared:
        path = base / str(entry["source"])
        if not path.is_dir():
            # Loud rather than silent. A configured corpus that is not there
            # is a deployment error, and treating it as "no corpus" makes the
            # agent refuse every question with nothing to explain why.
            raise CorpusError(
                f"tenant {tenant_id!r} declares corpus source {entry['source']!r} "
                f"but {path} does not exist"
            )
        sources.append(CorpusSource(doc_type=str(entry.get("doc_type", "policy")), path=path))
    return sources


def load_passages(tenant_id: str, root: Path | None = None) -> list[Passage]:
    passages: list[Passage] = []
    for source in sources_for(tenant_id, root):
        for path in sorted(source.path.rglob("*.md")):
            passages.extend(chunk_document(path))
    if not passages:
        raise CorpusError(f"tenant {tenant_id!r} has a configured corpus but it contains no documents")
    return passages


async def build_index(
    tenant_id: str,
    *,
    embedder: Embedder | None = None,
    root: Path | None = None,
    cache: bool = True,
) -> HybridIndex:
    if cache and tenant_id in _INDEXES:
        return _INDEXES[tenant_id]

    index = HybridIndex(load_passages(tenant_id, root), embedder or HashingEmbedder())
    await index.build()
    if cache:
        _INDEXES[tenant_id] = index
    return index


def clear_cache() -> None:
    _INDEXES.clear()


__all__ = [
    "CorpusError",
    "CorpusSource",
    "build_index",
    "chunk_corpus",
    "clear_cache",
    "load_passages",
    "sources_for",
]
