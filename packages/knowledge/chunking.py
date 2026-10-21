"""Splitting corpus documents into passages with stable ids.

A citation refers to a passage id, so that id has to survive re-ingestion of
an unchanged document. If ids were positional, adding a paragraph to the top
of a policy would renumber everything below it and silently invalidate every
stored citation and every eval fixture — the kind of breakage that shows up as
a reviewer clicking a citation and landing on the wrong text.

The id is therefore structural: document, version, section heading, and an
index where one section yields several passages. Reordering sections leaves
ids untouched, and so does fixing a typo.

Content changes are handled separately, by a ``content_hash`` on each passage
rather than by moving the id. A citation records the hash it was made against,
so a verifier can tell that the cited text has changed since — which is the
property that actually matters. Folding content into the id would conflate two
different events: "this rule moved" and "this rule was reworded". The first
should break nothing; the second should be detectable without breaking every
stored citation at once.

A substantive policy change bumps the document version, which changes every id
in it. That is the intended workflow: v3 and v4 of a refund policy are
different documents to cite.

Splitting is on headings rather than on a token count. A policy's sections are
the units a human cites — "the eligibility window says" — and a 512-token
window cutting across two rules produces a passage that supports neither.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
HEADING = re.compile(r"^##\s+(.+)$", re.MULTILINE)
SLUG_CHARS = re.compile(r"[^a-z0-9]+")

# Long enough that a single rule stays whole, short enough that a passage is
# quotable. Sections beyond this are split on paragraph boundaries.
MAX_PASSAGE_CHARS = 1200


@dataclass(frozen=True, slots=True)
class Passage:
    passage_id: str
    document_id: str
    doc_type: str
    title: str
    section: str | None
    text: str
    version: str
    content_hash: str
    effective_from: str | None = None
    effective_to: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "passage_id": self.passage_id,
            "document_id": self.document_id,
            "doc_type": self.doc_type,
            "title": self.title,
            "section": self.section,
            "text": self.text,
            "version": self.version,
            "content_hash": self.content_hash,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
        }


def slug(text: str, limit: int = 24) -> str:
    cleaned = SLUG_CHARS.sub("-", text.lower()).strip("-")
    return cleaned[:limit].strip("-") or "section"


def passage_id(document_id: str, version: str, section: str | None, index: int = 0) -> str:
    """A structural id: stable under reordering and under content edits.

    ``index`` disambiguates the parts of a section too long to be one passage.
    It is positional *within* the section only, so adding a section elsewhere
    in the document does not shift it.
    """
    section_part = slug(section) if section else "body"
    suffix = f".{index}" if index else ""
    return f"psg_{document_id}.{version}.{section_part}{suffix}"


def content_hash(text: str) -> str:
    """A fingerprint of the passage's exact text.

    Recorded alongside a citation so a verifier can detect that the cited
    wording changed, without the id having to move and invalidate everything.
    """
    normalised = " ".join(text.split())
    return hashlib.blake2b(normalised.encode(), digest_size=8).hexdigest()


def parse_frontmatter(source: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER.match(source)
    if match is None:
        return {}, source
    metadata = yaml.safe_load(match.group(1)) or {}
    return metadata, source[match.end() :]


def split_sections(body: str) -> list[tuple[str | None, str]]:
    """Split on level-two headings, keeping each heading with its text."""
    matches = list(HEADING.finditer(body))
    if not matches:
        text = body.strip()
        return [(None, text)] if text else []

    sections: list[tuple[str | None, str]] = []
    preamble = body[: matches[0].start()].strip()
    if preamble:
        sections.append((None, preamble))

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        text = body[start:end].strip()
        if text:
            sections.append((match.group(1).strip(), text))
    return sections


def split_long(text: str) -> list[str]:
    """Break an over-long section on paragraph boundaries, never mid-sentence."""
    if len(text) <= MAX_PASSAGE_CHARS:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for raw in text.split("\n\n"):
        paragraph = raw.strip()
        if not paragraph:
            continue
        if size and size + len(paragraph) > MAX_PASSAGE_CHARS:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def chunk_document(path: Path) -> list[Passage]:
    metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))

    document_id = str(metadata.get("document_id") or path.stem)
    version = str(metadata.get("version", "v1"))
    doc_type = str(metadata.get("doc_type", "policy"))
    title = str(metadata.get("title", document_id))
    effective_from = metadata.get("effective_from")
    effective_to = metadata.get("effective_to")

    passages: list[Passage] = []
    for section, text in split_sections(body):
        for index, part in enumerate(split_long(text)):
            normalised = " ".join(part.split())
            passages.append(
                Passage(
                    passage_id=passage_id(document_id, version, section, index),
                    document_id=document_id,
                    doc_type=doc_type,
                    title=title,
                    section=section,
                    text=normalised,
                    version=version,
                    content_hash=content_hash(normalised),
                    effective_from=str(effective_from) if effective_from else None,
                    effective_to=str(effective_to) if effective_to else None,
                    metadata={
                        k: v
                        for k, v in metadata.items()
                        if k
                        not in {
                            "document_id",
                            "doc_type",
                            "title",
                            "version",
                            "effective_from",
                            "effective_to",
                        }
                    },
                )
            )
    return passages


def chunk_corpus(root: Path) -> list[Passage]:
    passages: list[Passage] = []
    for path in sorted(root.rglob("*.md")):
        passages.extend(chunk_document(path))
    return passages
