"""Chunking, embedding and hybrid retrieval.

These test the machinery's structural properties — id stability, fusion,
thresholds, effective dating, and the empty result that drives the refusal
path. They do not test retrieval *quality*, which is a property of the real
embedding model and is measured in phase 8.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.knowledge.chunking import (
    chunk_document,
    content_hash,
    parse_frontmatter,
    passage_id,
    split_sections,
)
from packages.knowledge.corpus import CorpusError, load_passages, sources_for
from packages.knowledge.embeddings import HashingEmbedder, content_tokens, cosine
from packages.knowledge.retrieval import HybridIndex

CORPUS = Path(__file__).resolve().parents[3] / "infra" / "corpus" / "acme"
POLICY = CORPUS / "policy" / "refund-policy.md"


@pytest.fixture
async def index() -> HybridIndex:
    built = HybridIndex(load_passages("acme-payments"), HashingEmbedder())
    await built.build()
    return built


class TestStableIds:
    def test_ids_survive_re_ingestion(self) -> None:
        first = [p.passage_id for p in chunk_document(POLICY)]
        second = [p.passage_id for p in chunk_document(POLICY)]
        assert first == second

    def test_reordering_sections_changes_nothing(self, tmp_path: Path) -> None:
        # The property that matters: adding or moving a section must not
        # invalidate a citation to a different one.
        source = POLICY.read_text()
        metadata_block = source.split("---\n")[1]
        _, body = parse_frontmatter(source)
        sections = [(n, t) for n, t in split_sections(body) if n]

        rebuilt = tmp_path / "reordered.md"
        rebuilt.write_text(
            f"---\n{metadata_block}---\n\n"
            + "\n\n".join(f"## {n}\n\n{t}" for n, t in reversed(sections))
        )

        original = {p.passage_id: p.content_hash for p in chunk_document(POLICY)}
        after = {p.passage_id: p.content_hash for p in chunk_document(rebuilt)}
        assert original == after

    def test_rewording_changes_one_hash_and_no_id(self, tmp_path: Path) -> None:
        # The complement: an id stays put so citations keep resolving, while
        # the hash moves so a verifier can tell the text changed.
        edited = tmp_path / "edited.md"
        edited.write_text(
            POLICY.read_text().replace("5 to 7 working days.", "five to seven days.", 1)
        )

        original = {p.passage_id: p.content_hash for p in chunk_document(POLICY)}
        after = {p.passage_id: p.content_hash for p in chunk_document(edited)}

        assert set(original) == set(after), "ids must not move when text is reworded"
        changed = [k for k in original if original[k] != after[k]]
        assert changed == ["psg_refund-policy.v3.credit-timelines"]

    def test_the_id_carries_the_document_version(self) -> None:
        # A substantive change bumps the version, which changes every id in
        # the document — v3 and v4 of a policy are different things to cite.
        assert passage_id("refund-policy", "v3", "Credit timelines") != passage_id(
            "refund-policy", "v4", "Credit timelines"
        )

    def test_content_hash_ignores_whitespace_only_changes(self) -> None:
        assert content_hash("a  b\nc") == content_hash("a b c")


class TestEmbedder:
    async def test_related_text_scores_higher_than_unrelated(self) -> None:
        embedder = HashingEmbedder()
        query, related, unrelated = await embedder.embed(
            [
                "how long do refunds take to reach the customer",
                "card refunds reach the cardholder's statement in 5 to 7 working days",
                "a chargeback is a buyer disputing a capture through their issuing bank",
            ]
        )
        assert cosine(query, related) > cosine(query, unrelated)

    async def test_it_is_deterministic(self) -> None:
        first = await HashingEmbedder().embed(["refund policy"])
        second = await HashingEmbedder().embed(["refund policy"])
        assert first == second

    def test_function_words_are_dropped(self) -> None:
        # They carry no topical signal but do hash into dimensions, which gave
        # unrelated questions enough similarity to defeat the refusal path.
        assert content_tokens("what is the capital of France") == ["capital", "france"]


class TestHybridRetrieval:
    @pytest.mark.parametrize(
        "query,document",
        [
            ("How long do refunds take to reach the customer?", "refund-policy"),
            ("refund eligibility window after capture", "refund-policy"),
            ("why are my payouts blocked", "kyc-guide"),
            ("settlement still processing past its expected date", "inc-2208"),
            ("we received a dispute notice", "dispute-policy"),
            ("can support lift a payout hold", "kyc-guide"),
        ],
    )
    async def test_a_supported_question_finds_the_right_document(
        self, index: HybridIndex, query: str, document: str
    ) -> None:
        # Asserted at document level deliberately. Which *section* best answers
        # a loosely worded question is a ranking-quality judgement that belongs
        # in phase 8 against the real embedder — "we received a dispute notice"
        # is served correctly by either the definition or the evidence rules,
        # and pinning one makes the test an opinion rather than a check.
        hits = await index.search(query, top_k=3, min_score=0.45)
        assert hits, f"{query!r} should find something"
        assert document in {h.passage.document_id for h in hits}

    @pytest.mark.parametrize(
        "query,expected",
        [
            (
                "how many days after capture can a refund be initiated",
                "psg_refund-policy.v3.eligibility-window",
            ),
            (
                "how many working days until a card refund is credited",
                "psg_refund-policy.v3.credit-timelines",
            ),
        ],
    )
    async def test_a_precise_question_finds_its_exact_passage(
        self, index: HybridIndex, query: str, expected: str
    ) -> None:
        # Where the question names the rule, the top hit should be the rule.
        hits = await index.search(query, top_k=3, min_score=0.45)
        assert hits
        assert hits[0].passage.passage_id == expected

    @pytest.mark.parametrize(
        "query",
        [
            "zebra husbandry techniques",
            "what is the capital of France",
            "please recommend a restaurant",
            "how do I train for a marathon",
        ],
    )
    async def test_an_unsupported_question_returns_nothing(
        self, index: HybridIndex, query: str
    ) -> None:
        # The refusal path depends entirely on this. A retriever that always
        # returns something makes the agent incapable of saying the policy
        # does not cover a question.
        assert await index.search(query, top_k=3, min_score=0.45) == []

    async def test_doc_type_filtering(self, index: HybridIndex) -> None:
        hits = await index.search("refund", top_k=5, doc_types={"policy"})
        assert hits
        assert all(h.passage.doc_type == "policy" for h in hits)

    async def test_a_passage_not_yet_effective_is_not_returned(self, index: HybridIndex) -> None:
        # A policy that takes effect next month must not be cited as current.
        assert await index.search("refund credit timelines", on_date="2020-01-01") == []

    async def test_results_are_stable_for_an_identical_query(self, index: HybridIndex) -> None:
        query = "how long do refunds take"
        first = await index.search(query, top_k=4)
        second = await index.search(query, top_k=4)
        assert [h.passage.passage_id for h in first] == [h.passage.passage_id for h in second]

    async def test_min_score_filters(self, index: HybridIndex) -> None:
        loose = await index.search("refund", top_k=10)
        strict = await index.search("refund", top_k=10, min_score=0.99)
        assert len(strict) <= len(loose)
        assert all(h.score >= 0.99 for h in strict)

    async def test_an_empty_query_returns_nothing(self, index: HybridIndex) -> None:
        assert await index.search("   ", top_k=5) == []


class TestCorpusConfiguration:
    def test_sources_come_from_the_tenant_file(self) -> None:
        # Not from a convention mapping tenant id to directory name, which is
        # how the corpus came to live in `acme` while the tenant is
        # `acme-payments` and every query silently refused.
        sources = sources_for("acme-payments")
        assert {s.doc_type for s in sources} == {"policy", "help_centre", "incident_notice"}
        assert all(s.path.is_dir() for s in sources)

    def test_an_unknown_tenant_fails_loudly(self) -> None:
        with pytest.raises(CorpusError):
            sources_for("no-such-tenant")

    def test_the_corpus_covers_the_grounded_scenarios(self) -> None:
        ids = {p.passage_id for p in load_passages("acme-payments")}
        for required in (
            "psg_refund-policy.v3.credit-timelines",
            "psg_refund-policy.v3.eligibility-window",
            "psg_settlement-policy.v2.delays",
            "psg_kyc-guide.v2.what-to-do-next",
            "psg_kyc-guide.v2.what-support-can-and-can",
            "psg_dispute-policy.v2.evidence",
        ):
            assert required in ids, f"{required} is cited by a scenario but not in the corpus"
