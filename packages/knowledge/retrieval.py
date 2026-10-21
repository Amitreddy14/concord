"""Hybrid retrieval: BM25 and dense vectors, fused by reciprocal rank.

Neither half is sufficient on its own, and the failures are different.

Lexical search misses paraphrase. A merchant asking "when will my customer get
their money back" shares almost no vocabulary with a passage headed "credit
timelines", and BM25 scores it near zero.

Dense search misses exact tokens. A query naming ``order 8842`` or a UTR has
its distinguishing term smeared into a direction in embedding space, and a
passage about a different reference ranks just as well.

Reciprocal rank fusion combines them without needing their scores to be
comparable — which they are not, since BM25 is unbounded and cosine lives in
[-1, 1]. RRF uses only each retriever's *ordering*, so no normalisation
constant has to be tuned and no retriever's score distribution can dominate
the other's by accident.

The relevance score returned to callers is deliberately derived from the fused
rank rather than from either raw score, because the tenant's ``min_passage_score``
threshold has to mean one thing. A caller comparing against a number that
sometimes came from BM25 and sometimes from cosine would be comparing against
noise.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from packages.knowledge.chunking import Passage
from packages.knowledge.embeddings import STOPWORDS, Embedder, content_tokens, cosine

# The standard RRF constant. Large enough that the top few ranks are not
# wildly over-weighted, small enough that rank 1 still beats rank 10 clearly.
RRF_K = 60

BM25_K1 = 1.5
BM25_B = 0.75

# A dense retriever ranks every passage, including for a query about nothing
# in the corpus. RRF then turns "least irrelevant" into a rank, and a rank into
# a score, so an unsupported question comes back with confident-looking
# results.
#
# That matters more here than elsewhere: the refusal path is triggered by an
# empty result, so a retriever that never returns empty means the agent never
# refuses. A passage must clear an absolute similarity floor before it is
# allowed a rank at all — and the floor comes from the embedder, since it is a
# property of the embedding space rather than of the fusion.

@dataclass(frozen=True, slots=True)
class Hit:
    passage: Passage
    score: float
    lexical_rank: int | None
    dense_rank: int | None

    @property
    def found_by_both(self) -> bool:
        return self.lexical_rank is not None and self.dense_rank is not None


def content_terms(text: str) -> list[str]:
    """Shared with the embedder, so both halves agree on what carries signal."""
    return content_tokens(text)


@dataclass
class BM25:
    """Okapi BM25 over the passage corpus."""

    passages: list[Passage]
    _documents: list[list[str]] = field(default_factory=list, init=False)
    _frequencies: list[Counter[str]] = field(default_factory=list, init=False)
    _document_frequency: Counter[str] = field(default_factory=Counter, init=False)
    _average_length: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._documents = [content_terms(p.text + " " + (p.section or "")) for p in self.passages]
        self._frequencies = [Counter(d) for d in self._documents]
        for frequency in self._frequencies:
            self._document_frequency.update(frequency.keys())
        lengths = [len(d) for d in self._documents]
        self._average_length = (sum(lengths) / len(lengths)) if lengths else 0.0

    def score(self, query: str) -> list[tuple[int, float]]:
        terms = content_terms(query)
        total = len(self._documents)
        if not terms or total == 0:
            return []

        scored: list[tuple[int, float]] = []
        for index, frequency in enumerate(self._frequencies):
            length = len(self._documents[index])
            value = 0.0
            for term in terms:
                occurrences = frequency.get(term, 0)
                if occurrences == 0:
                    continue
                containing = self._document_frequency[term]
                idf = math.log(1 + (total - containing + 0.5) / (containing + 0.5))
                denominator = occurrences + BM25_K1 * (
                    1 - BM25_B + BM25_B * length / (self._average_length or 1)
                )
                value += idf * (occurrences * (BM25_K1 + 1)) / denominator
            if value > 0:
                scored.append((index, value))

        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored


@dataclass
class HybridIndex:
    """The retrieval index for one tenant's corpus."""

    passages: list[Passage]
    embedder: Embedder
    _bm25: BM25 = field(init=False)
    _vectors: list[list[float]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._bm25 = BM25(self.passages)

    async def build(self) -> None:
        """Embed the corpus. Separate from __init__ because it is async."""
        texts = [f"{p.title}. {p.section or ''}. {p.text}" for p in self.passages]
        self._vectors = await self.embedder.embed(texts)

    async def search(
        self,
        query: str,
        *,
        top_k: int = 6,
        doc_types: set[str] | None = None,
        min_score: float | None = None,
        on_date: str | None = None,
    ) -> list[Hit]:
        if not query.strip() or not self.passages:
            return []

        eligible = {
            index
            for index, passage in enumerate(self.passages)
            if (not doc_types or passage.doc_type in doc_types)
            and _is_effective(passage, on_date)
        }
        if not eligible:
            return []

        lexical = [(i, s) for i, s in self._bm25.score(query) if i in eligible]
        lexical_rank = {index: rank for rank, (index, _) in enumerate(lexical, start=1)}

        dense_rank: dict[int, int] = {}
        if self._vectors:
            (query_vector,) = await self.embedder.embed([query])
            similarities = sorted(
                ((i, cosine(query_vector, self._vectors[i])) for i in eligible),
                key=lambda pair: (-pair[1], pair[0]),
            )
            floor = getattr(self.embedder, "similarity_floor", 0.25)
            above_floor = [
                (index, score) for index, score in similarities if score >= floor
            ]
            dense_rank = {
                index: rank for rank, (index, _) in enumerate(above_floor, start=1)
            }

        fused: dict[int, float] = {}
        for index, rank in lexical_rank.items():
            fused[index] = fused.get(index, 0.0) + 1 / (RRF_K + rank)
        for index, rank in dense_rank.items():
            fused[index] = fused.get(index, 0.0) + 1 / (RRF_K + rank)

        if not fused:
            return []

        ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))

        # Normalise against the best score achievable *for this query*, which
        # is rank 1 from each retriever that actually contributed.
        #
        # Normalising against two retrievers unconditionally penalises a query
        # the dense half found nothing for: its best possible hit would score
        # 0.5 however good the lexical match was, which puts a perfectly
        # appropriate passage within a rounding error of a configured
        # threshold. An exact-term query — an order reference, a UTR — is
        # precisely the case dense retrieval misses and lexical nails, so
        # scoring it as half-confident is backwards.
        contributing = (1 if lexical_rank else 0) + (1 if dense_rank else 0)
        ceiling = contributing / (RRF_K + 1)
        hits = [
            Hit(
                passage=self.passages[index],
                score=round(value / ceiling, 4),
                lexical_rank=lexical_rank.get(index),
                dense_rank=dense_rank.get(index),
            )
            for index, value in ordered
        ]
        if min_score is not None:
            hits = [h for h in hits if h.score >= min_score]
        return hits[:top_k]


def _is_effective(passage: Passage, on_date: str | None) -> bool:
    """Whether a passage states current policy.

    A superseded refund window must not be citable as if it still applied.
    """
    if on_date is None:
        return True
    if passage.effective_from and passage.effective_from > on_date:
        return False
    return not (passage.effective_to and passage.effective_to < on_date)
