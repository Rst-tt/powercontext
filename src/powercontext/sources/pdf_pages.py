# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Capture every physical page of a financial report as retrievable evidence."""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Iterable, Sequence
from typing import Any, Final
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, model_validator

from powercontext.artifacts import MemoryCitation
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.definitions import SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization

PDF_PAGE_SOURCE_NAME: Final = "pdf-page"
PDF_PAGE_SOURCE_VERSION: Final = "1"

#: The Memory entry body bound. One projected page must fit in one Memory entry.
MAX_PDF_PAGE_TEXT_BYTES: Final = 8_192

#: Retrieval tuning. One question is answered from a small number of evidence pages.
_SEARCH_POOL_MULTIPLIER: Final = 4
_SEARCH_POOL_FLOOR: Final = 60
_RRF_CONSTANT: Final = 60

_TOKEN_PATTERN: Final = re.compile(r"[a-z]+|\d+")
_SENTENCE_PATTERN: Final = re.compile(r"(?<=[.!?])\s+|\n+")

__all__ = [
    "MAX_PDF_PAGE_TEXT_BYTES",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDF_PAGE_SOURCE_VERSION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageTextEvidenceProjection",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]


class PDFDocumentInput(BaseModel):
    """Complete original PDF bytes plus the document-level identity key."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page of one document revision with its extractable body."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    document_sha256: str
    page_number: int
    text: str

    @model_validator(mode="after")
    def validate_page(self) -> PDFPageInput:
        if self.page_number < 1:
            raise ValueError("page_number must count physical pages from 1")  # noqa: TRY003
        if not _is_sha256(self.document_sha256):
            raise ValueError("document_sha256 must be a lowercase hexadecimal digest")  # noqa: TRY003
        return self


class PDFPageSource(Source):
    """A captured physical page addressed by document, revision and page number."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str

    @model_validator(mode="after")
    def validate_identity(self) -> PDFPageSource:
        expected = pdf_page_source_id(self.document_id, self.document_sha256, self.page_number)
        if self.name != expected or self.source_id != expected:
            raise ValueError("PDF page Source must use its page identity for name and source_id")  # noqa: TRY003
        return self


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build the stable, injective and bounded identity of one physical page."""

    _validate_document_id(document_id)
    if not _is_sha256(document_sha256):
        raise ValueError("document_sha256 must be a lowercase hexadecimal digest")  # noqa: TRY003
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page_number must count physical pages from 1")  # noqa: TRY003
    identity = f"{PDF_PAGE_SOURCE_NAME}:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(identity) <= MAX_SOURCE_ID_LENGTH:
        return identity
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{PDF_PAGE_SOURCE_NAME}=sha256:{digest}"


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of one complete PDF before returning any page."""

    document_id = _validate_document_id(value.document_id)
    content = _validate_content(value.content)
    digest = hashlib.sha256(content).hexdigest()
    texts = _read_page_texts(content)
    return tuple(
        PDFPageInput(
            document_id=document_id,
            document_sha256=digest,
            page_number=page_number,
            text=_bounded_text(document_id, text),
        )
        for page_number, text in enumerate(texts, start=1)
    )


class PDFPageSourceAdapter:
    """Resolve and read one captured page without altering its extracted body."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        identity = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one complete physical page as canonical textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    version=PDF_PAGE_SOURCE_VERSION,
    projections=(PDFPageTextEvidenceProjection(),),
)


async def search_pdf_pages(
    memory: Any,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Return at most ``limit`` distinct page citations for one natural-language question.

    The scoped Memory is the only source of candidates: every returned citation is an
    exact Memory entry that a caller can read back through ``memory.get``. Several
    offline query views of the same question are searched and fused, because one
    question rarely matches a single page with every one of its terms.
    """

    if limit < 1:
        return ()
    text = question.strip() if isinstance(question, str) else ""
    if not text:
        return ()

    from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest

    pool = max(limit * _SEARCH_POOL_MULTIPLIER, _SEARCH_POOL_FLOOR)
    candidates: dict[tuple[str, str], _Candidate] = {}
    for query in _query_variants(text):
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=pool))
        for rank, hit in enumerate(page.hits, start=1):
            identity = (hit.entry_id, hit.entry_version_id)
            candidate = candidates.get(identity)
            if candidate is None:
                candidate = _Candidate(hit=hit, ranks=[])
                candidates[identity] = candidate
            candidate.ranks.append(rank)

    ranked = _rank_candidates(candidates.values(), text)
    selected: list[MemoryCitation] = []
    seen_sources: set[str] = set()
    for candidate in ranked:
        if len(selected) >= limit:
            break
        citation = MemoryCitation(
            memory_ref=candidate.hit.memory_ref,
            entry_id=candidate.hit.entry_id,
            entry_version_id=candidate.hit.entry_version_id,
        )
        try:
            record = await memory.get(GetMemoryEntryRequest(citation=citation))
        except Exception:  # noqa: BLE001 - a stale candidate is simply not evidence
            continue
        sources = record.entry.sources
        if len(sources) != 1:
            continue
        source_id = sources[0].source_id
        if source_id in seen_sources:
            continue
        seen_sources.add(source_id)
        selected.append(citation)
    return tuple(selected)


class _Candidate:
    """One retrieved Memory entry together with its ranks across query views."""

    __slots__ = ("hit", "ranks")

    def __init__(self, hit: Any, ranks: list[int]) -> None:
        self.hit = hit
        self.ranks = ranks


def _query_variants(question: str) -> tuple[str, ...]:
    """Build the offline query views of one question."""

    variants: list[str] = []
    whole = " ".join(question.split())
    if whole:
        variants.append(whole)
    for sentence in _SENTENCE_PATTERN.split(question):
        cleaned = " ".join(sentence.split())
        if cleaned and cleaned not in variants:
            variants.append(cleaned)
    return tuple(variants)


def _rank_candidates(candidates: Iterable[_Candidate], question: str) -> list[_Candidate]:
    """Fuse the query views and prefer pages that cover more of the question."""

    terms = _distinct_terms(question)
    scored: list[tuple[float, float, _Candidate]] = []
    for candidate in candidates:
        fused = sum(1.0 / (_RRF_CONSTANT + rank) for rank in candidate.ranks)
        coverage = _coverage(candidate.hit.text, terms)
        scored.append((fused + 0.5 * coverage, fused, candidate))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2].hit.entry_id))
    return [candidate for _score, _fused, candidate in scored]


def _distinct_terms(question: str) -> tuple[str, ...]:
    """Return the distinct lowercase terms of one question."""

    return tuple(dict.fromkeys(_TOKEN_PATTERN.findall(question.lower())))


def _coverage(text: str, terms: Sequence[str]) -> float:
    """Return the share of question terms that one candidate body contains."""

    if not terms:
        return 0.0
    tokens = set(_TOKEN_PATTERN.findall(text.lower()))
    return sum(1 for term in terms if term in tokens) / len(terms)


def _validate_document_id(document_id: str) -> str:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    return document_id


def _validate_content(content: object) -> bytes:
    if not isinstance(content, bytes | bytearray | memoryview):
        raise ValueError("PDF content must be bytes")  # noqa: TRY003, TRY004
    data = bytes(content)
    if not data:
        raise ValueError("PDF content must not be empty")  # noqa: TRY003
    return data


def _read_page_texts(content: bytes) -> tuple[str, ...]:
    """Extract the body of every physical page, or fail without returning a page."""

    try:
        from pypdf import PdfReader
    except ImportError as error:  # pragma: no cover - the locked environment ships pypdf
        raise ImportError("PDF parsing requires the pypdf package") from error

    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            if not reader.decrypt(""):
                raise ValueError("PDF content is encrypted with an unknown password")  # noqa: TRY003
        return tuple(_normalize_text(page.extract_text() or "") for page in reader.pages)
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("PDF content could not be parsed completely") from error


def _normalize_text(text: str) -> str:
    """Return one page body with stable line endings."""

    return text.replace("\r\n", "\n").replace("\r", "\n")


def _bounded_text(document_id: str, text: str) -> str:
    """Keep one page body inside the Memory entry bound without losing whole pages."""

    budget = MAX_PDF_PAGE_TEXT_BYTES - len(document_id.encode("utf-8")) - 1
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    truncated = encoded[:budget].decode("utf-8", errors="ignore")
    while len(f"{document_id}\n{truncated}".encode("utf-8")) > MAX_PDF_PAGE_TEXT_BYTES:
        truncated = truncated[:-1]
    return truncated


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )
