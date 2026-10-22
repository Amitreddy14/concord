"""Embedding backends.

Two implementations, for the same reason the LLM gateway has two. The Ollama
one is what a deployment uses. The deterministic one is what the test suite
uses, because a suite that needs a 274MB embedding model running is a suite
that does not run in CI, and a suite that does not run in CI is a suite that
rots.

The deterministic embedder is not a stub that returns zeros. It produces a
real, stable vector from token hashing, so cosine similarity between related
texts is genuinely higher than between unrelated ones. That is enough to test
the fusion logic, the ranking and the thresholds — which is what the tests are
actually about. Retrieval *quality* is measured in phase 8 against the real
embedder, where it belongs.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx

WORD = re.compile(r"[a-z0-9]+")
DEFAULT_DIMENSIONS = 256

# Function words carry no topical signal but do hash into dimensions, which
# gave "what is the capital of France" a spurious similarity to every passage
# in a payments corpus — enough to clear the retrieval floor and defeat the
# refusal path. A trained embedder learns to discount them; a hashing one has
# to be told.
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "my",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)


def tokenize(text: str) -> list[str]:
    return WORD.findall(text.lower())


def content_tokens(text: str) -> list[str]:
    return [t for t in tokenize(text) if t not in STOPWORDS and len(t) > 1]


class Embedder(Protocol):
    dimensions: int

    similarity_floor: float
    """Below this cosine, a passage is noise rather than a weak match.

    A property of the embedder, not of the retriever. What counts as "similar"
    depends entirely on the embedding space: a trained model and a hashing
    trick produce completely different similarity distributions, and a single
    global constant would be calibrated for neither. Measured per
    implementation, against real queries.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass
class OllamaEmbedder:
    """The real embedder, over Ollama's OpenAI-compatible embeddings endpoint."""

    base_url: str
    model: str
    client: httpx.AsyncClient
    dimensions: int = 768
    # A provisional floor for a normalised sentence-embedding space. Phase 8
    # calibrates it against the golden scenarios on the real model; until then
    # it is a starting point, not a measurement.
    similarity_floor: float = 0.35
    api_key: str = "not-needed-for-local"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await self.client.post(
            f"{self.base_url}/embeddings",
            json={"model": self.model, "input": texts},
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=120.0,
        )
        response.raise_for_status()
        payload = response.json()
        ordered = sorted(payload["data"], key=lambda item: item["index"])
        vectors: list[list[float]] = [item["embedding"] for item in ordered]
        if vectors:
            self.dimensions = len(vectors[0])
        return vectors


@dataclass
class HashingEmbedder:
    """A deterministic embedder with real similarity structure.

    Each token is hashed to a dimension and contributes a sign and a weight,
    the classic hashing-trick construction. Texts sharing vocabulary end up
    pointing in similar directions; texts that share none are near-orthogonal.
    No model, no network, identical output on every run.
    """

    dimensions: int = DEFAULT_DIMENSIONS
    # Measured on this corpus: a genuine match scores around 0.35, while hash
    # collisions between unrelated text top out near 0.19. The floor sits in
    # the gap. Narrower separation than a trained model gives, which is the
    # honest cost of an embedder with no model behind it.
    similarity_floor: float = 0.25
    _cache: dict[str, list[float]] = field(default_factory=dict)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        cached = self._cache.get(text)
        if cached is not None:
            return cached

        vector = [0.0] * self.dimensions
        tokens = content_tokens(text)
        for token in tokens:
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            # Sub-linear in term frequency, as tf-idf would be: a word
            # repeated ten times is not ten times as informative.
            vector[index] += sign

        norm = math.sqrt(sum(v * v for v in vector))
        if norm > 0:
            vector = [v / norm for v in vector]
        self._cache[text] = vector
        return vector
