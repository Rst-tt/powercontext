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
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, unquote

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

#: Retrieval tuning. A question is answered from a handful of evidence pages, so the
#: ranking weighs structural signals - a statement page, a section heading, a numeric
#: table - next to plain term overlap, and prefers pages of the document it names. The
#: statement prior is the strongest of them: when a question asks for a reported line
#: item the statement page is the evidence, and the pages that only discuss it are not.
_STATEMENT_WEIGHT: Final = 16.0
_HEADING_WEIGHT: Final = 1.0
_DIGIT_WEIGHT: Final = 3.0
_DOCUMENT_WEIGHT: Final = 20.0
_BM25_K1: Final = 1.2
#: Report pages run long, so length normalization stays gentle: a note that answers the
#: question in its last paragraph must still outrank a short page that only repeats it.
_BM25_B: Final = 0.4


#: A statement page announces itself in its header, before any narrative section.
_STATEMENT_HEAD_CHARS: Final = 220
_STATEMENT_MARKERS: Final = (
    "balancesheet",
    "balancesheets",
    "consolidatedbalancesheet",
    "consolidatedbalancesheets",
    "statementofincome",
    "statementsofincome",
    "consolidatedstatementofincome",
    "consolidatedstatementsofincome",
    "statementofoperations",
    "statementsofoperations",
    "consolidatedstatementsofoperations",
    "statementofcashflows",
    "statementsofcashflows",
    "consolidatedstatementsofcashflows",
    "statementoffinancialposition",
    "statementsoffinancialposition",
    "consolidatedstatementsoffinancialposition",
    "statementofearnings",
    "statementsofearnings",
)

_TERM_PATTERN: Final = re.compile(r"[a-z]+|\d+")
_SENTENCE_PATTERN: Final = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE_PATTERN: Final = re.compile(r"\s+")
_STRUCTURAL_HEADING: Final = re.compile(r"^(?:item\s+\d|part\s+[ivx]+|note\s+\d)", re.IGNORECASE)

#: A question about a report also carries instructions about how to answer it. Those
#: sentences describe the analyst, not the page, so they must not weigh on ranking.
_INSTRUCTION_PATTERN: Final = re.compile(
    r"\bround(?:ed)?\b[^.]*\bdecimal\b"
    r"|\bdecimal places\b"
    r"|\bbase[ds]? (?:your|on your) judgment\b"
    r"|\binformation provided\b"
    r"|\bpublicly available\b"
    r"|\bplease state\b"
    r"|\bnot a relevant metric\b"
    r"|\bassume that\b"
    r"|\bperspective of an? \w+\b"
    r"|\bfiscal year end\b",
    re.IGNORECASE,
)

_FUNCTION_WORDS: Final = frozenset([
    "a",
    "an",
    "the",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "for",
    "with",
    "from",
    "and",
    "or",
    "but",
    "if",
    "then",
    "than",
    "that",
    "this",
    "these",
    "those",
    "it",
    "its",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "do",
    "does",
    "did",
    "has",
    "have",
    "had",
    "can",
    "could",
    "would",
    "should",
    "will",
    "shall",
    "may",
    "might",
    "must",
    "not",
    "no",
    "as",
    "so",
    "such",
    "what",
    "which",
    "who",
    "whom",
    "whose",
    "when",
    "where",
    "why",
    "how",
    "we",
    "our",
    "you",
    "your",
    "they",
    "their",
    "he",
    "she",
    "him",
    "her",
    "me",
    "my",
    "us",
    "them",
    "there",
    "here",
    "also",
    "any",
    "all",
    "each",
    "every",
    "both",
    "either",
    "neither",
    "more",
    "most",
    "other",
    "some",
    "many",
    "much",
    "few",
    "less",
    "least",
    "very",
    "just",
    "only",
    "about",
    "over",
    "under",
    "into",
    "onto",
    "out",
    "up",
    "down",
    "off",
    "per",
    "via",
    "vs",
    "versus",
    "between",
    "during",
    "after",
    "before",
    "since",
    "while",
    "through",
    "across",
    "among",
    "within",
    "without",
])
_META_WORDS: Final = frozenset([
    "answer",
    "respond",
    "response",
    "assume",
    "assumed",
    "assumption",
    "analyst",
    "banker",
    "perspective",
    "round",
    "rounded",
    "decimal",
    "place",
    "places",
    "usd",
    "million",
    "millions",
    "billion",
    "billions",
    "primarily",
    "base",
    "based",
    "judgment",
    "publicly",
    "available",
    "information",
    "provided",
    "please",
    "state",
    "two",
    "relevant",
    "metric",
    "fiscal",
    "year",
    "end",
    "fye",
    "fy",
    "fq",
    "quarter",
])
_STOP_TERMS: Final = _FUNCTION_WORDS | _META_WORDS

_WORD_PATTERN: Final = re.compile(r"[0-9A-Za-z][A-Za-z0-9&.'\u2019-]*")
_POSSESSIVE_PATTERN: Final = re.compile(r"['\u2019]s$")
_WORD_CHARACTERS_PATTERN: Final = re.compile(r"[^a-z0-9]+")
_YEAR_PATTERN: Final = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_QUARTER_PATTERN: Final = re.compile(r"\bq([1-4])\b", re.IGNORECASE)

#: Capitalised words that shape a question instead of naming an issuer.
_NON_NAME_WORDS: Final = frozenset([
    "which",
    "what",
    "how",
    "does",
    "do",
    "is",
    "are",
    "was",
    "were",
    "will",
    "can",
    "could",
    "should",
    "would",
    "the",
    "in",
    "for",
    "on",
    "by",
    "at",
    "to",
    "of",
    "and",
    "or",
    "if",
    "when",
    "where",
    "who",
    "why",
    "here",
    "this",
    "that",
    "these",
    "those",
    "as",
    "while",
    "during",
    "between",
    "after",
    "before",
    "since",
    "from",
    "with",
    "per",
    "plus",
    "versus",
    "vs",
    "fy",
    "fq",
    "gaap",
    "us",
    "usa",
    "usd",
    "eps",
    "roa",
    "roe",
    "ebitda",
    "cagr",
    "cpi",
    "gbp",
    "eur",
    "base",
    "based",
    "roughly",
    "address",
    "real",
    "sales",
    "total",
    "average",
    "net",
    "gross",
    "operating",
    "adjusted",
    "non",
    "pro",
    "forma",
    "company",
    "corporation",
    "inc",
    "its",
    "it",
    "their",
    "they",
    "we",
    "our",
    "you",
    "your",
    "please",
    "state",
    "assume",
    "round",
    "decimal",
    "places",
    "answer",
    "judgment",
    "information",
    "publicly",
    "available",
    "fiscal",
    "year",
    "financial",
    "statement",
    "statements",
    "share",
    "shares",
    "stock",
    "price",
    "value",
    "market",
    "revenue",
    "income",
    "cash",
    "flow",
    "flows",
    "margin",
])

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
    "pdf_page_identity",
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


def pdf_page_identity(source_id: str) -> tuple[str, int] | None:
    """Read the document and physical page back from one captured page identity."""

    prefix = f"{PDF_PAGE_SOURCE_NAME}:"
    if not source_id.startswith(prefix):
        return None
    parts = source_id[len(prefix) :].split(":")
    if len(parts) != 3:
        return None
    document_id, digest, number = parts
    if not _is_sha256(digest) or not number.isdigit() or int(number) < 1:
        return None
    return unquote(document_id), int(number)


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


@dataclass(frozen=True, slots=True)
class _Page:
    """One distinct captured page together with its exact Memory citation."""

    citation: MemoryCitation
    document_id: str
    page_number: int
    body: str


class _Corpus:
    """Token statistics over every captured page reachable from one scoped Memory."""

    def __init__(self, pages: Sequence[_Page]) -> None:
        self.pages: tuple[_Page, ...] = tuple(pages)
        self.tokens = tuple(tuple(_TERM_PATTERN.findall(page.body.lower())) for page in self.pages)
        self.counts = tuple(Counter(tokens) for tokens in self.tokens)
        self.headings = tuple(_heading_terms(page.body) for page in self.pages)
        self.lengths = tuple(len(tokens) for tokens in self.tokens)
        self.average_length = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        frequency: Counter[str] = Counter()
        for tokens in self.tokens:
            frequency.update(set(tokens))
        size = len(self.pages)
        self.idf = {term: math.log(1 + (size - count + 0.5) / (count + 0.5)) for term, count in frequency.items()}

    def bm25(self, index: int, terms: Sequence[str]) -> float:
        counts = self.counts[index]
        normalization = _BM25_K1 * (1 - _BM25_B + _BM25_B * self.lengths[index] / (self.average_length or 1.0))
        total = 0.0
        for term in terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            total += self.idf.get(term, 0.0) * frequency * (_BM25_K1 + 1) / (frequency + normalization)
        return total

    def heading_overlap(self, index: int, terms: Sequence[str]) -> float:
        heading = self.headings[index]
        return sum(self.idf.get(term, 0.0) for term in _distinct(terms) if term in heading)

    def digit_share(self, index: int) -> float:
        tokens = self.tokens[index]
        if not tokens:
            return 0.0
        return sum(1 for token in tokens if token.isdigit()) / len(tokens)


async def search_pdf_pages(
    memory: Any,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Return at most ``limit`` distinct page citations for one natural-language question.

    The scoped Memory is the only source of evidence: the pages are read from it, and
    every citation that leaves this function has been read back through ``memory.get``.
    Ranking is offline work over those exact entries, because one question rarely
    matches a page with every one of its terms.
    """

    if limit < 1:
        return ()
    text = question.strip() if isinstance(question, str) else ""
    if not text:
        return ()
    corpus = await _load_corpus(memory)
    if not corpus.pages:
        return ()
    terms = _query_terms(text)
    scores = _document_scores(text, corpus.pages)
    return await _citations(memory, _rank(corpus, terms, scores), limit)


async def _load_corpus(memory: Any) -> _Corpus:
    """Read every captured page of the scope through its public Memory listing."""

    listing = await memory.list()
    pages: list[_Page] = []
    seen: set[str] = set()
    for record in listing.entries:
        entry = record.entry
        if len(entry.sources) != 1:
            continue
        source_id = entry.sources[0].source_id
        identity = pdf_page_identity(source_id)
        if identity is None or source_id in seen:
            continue
        seen.add(source_id)
        document_id, page_number = identity
        pages.append(
            _Page(
                citation=record.citation,
                document_id=document_id,
                page_number=page_number,
                body=_page_body(document_id, entry.text),
            )
        )
    return _Corpus(pages)


def _rank(corpus: _Corpus, terms: Sequence[str], documents: dict[str, float]) -> list[_Page]:
    """Order every page of the scope by how likely it carries the answer."""

    best = max(documents.values(), default=0.0)
    distinct = _distinct(terms)
    scored: list[tuple[float, str, int, int, _Page]] = []
    for index, page in enumerate(corpus.pages):
        #: A structural prior only refines a page that answers something: a page that
        #: shares no term with the question is not evidence, whatever it presents.
        matched = any(term in corpus.counts[index] for term in distinct)
        score = (
            corpus.bm25(index, terms)
            + _HEADING_WEIGHT * corpus.heading_overlap(index, terms)
            + _DIGIT_WEIGHT * corpus.digit_share(index)
            + (_STATEMENT_WEIGHT if matched and _is_statement_page(page.body) else 0.0)
        )
        if best:
            score += _DOCUMENT_WEIGHT * documents.get(page.document_id, 0.0) / best
        scored.append((score, page.document_id, page.page_number, index, page))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[4] for item in scored]


def _distinct(terms: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(terms))


async def _citations(memory: Any, ranked: Sequence[_Page], limit: int) -> tuple[MemoryCitation, ...]:
    """Keep the best ranked pages that the Memory still answers for, one per page."""

    from powercontext.builtin.runtime import GetMemoryEntryRequest

    stale = _stale_citation_errors()
    selected: list[MemoryCitation] = []
    for candidate in ranked:
        if len(selected) >= limit:
            break
        try:
            await memory.get(GetMemoryEntryRequest(citation=candidate.citation))
        except stale:
            continue
        selected.append(candidate.citation)
    return tuple(selected)


def _stale_citation_errors() -> tuple[type[BaseException], ...]:
    from powercontext.builtin.artifacts.memory import InvalidMemoryCitationError, MemoryEntryNotFoundError

    return (InvalidMemoryCitationError, MemoryEntryNotFoundError)


def _query_terms(question: str) -> tuple[str, ...]:
    """Return the distinct content terms of one question after its instructions."""

    sentences = [
        sentence for sentence in _SENTENCE_PATTERN.split(question) if not _INSTRUCTION_PATTERN.search(sentence)
    ]
    text = _POSSESSIVE_PATTERN.sub("", " ".join(sentences).lower())
    #: A single letter of "M&A" or "3M's" matches half of a report and says nothing on its own.
    terms = [term for term in _TERM_PATTERN.findall(text) if len(term) > 1]
    return tuple(dict.fromkeys(term for term in terms if term not in _STOP_TERMS))


def _document_scores(question: str, pages: Sequence[_Page]) -> dict[str, float]:
    """Score how strongly the question names each document of the scope."""

    names = _proper_names(question)
    years = set(_YEAR_PATTERN.findall(question))
    quarters = set(_QUARTER_PATTERN.findall(question))
    scores: dict[str, float] = {}
    for document_id in {page.document_id for page in pages}:
        identifier = _normalize_name(document_id)
        score = sum(3.0 + 0.1 * len(name) for name in names if name in identifier)
        score += sum(1.0 for year in years if year in identifier)
        score += sum(1.0 for quarter in quarters if f"q{quarter}" in identifier)
        scores[document_id] = score
    return scores


def _proper_names(question: str) -> tuple[str, ...]:
    """Return the normalized name phrases of one question, without possessives."""

    names: list[str] = []
    run: list[str] = []
    for match in _WORD_PATTERN.finditer(question):
        word = _POSSESSIVE_PATTERN.sub("", match.group(0))
        if _is_name_word(word):
            run.append(word)
            continue
        names.extend(_name_variants(run))
        run = []
    names.extend(_name_variants(run))
    return tuple(dict.fromkeys(name for name in names if name))


def _name_variants(words: Sequence[str]) -> list[str]:
    """Return one joined phrase plus its individual words, all normalized.

    A two-character name still identifies a document: "3M" is a company, and dropping
    names that short leaves every question about it without a document to score.
    """

    normalized = [_normalize_name(word) for word in words]
    normalized = [item for item in normalized if len(item) >= 2]
    if not normalized:
        return []
    variants: list[str] = []
    joined = "".join(normalized)
    if len(joined) >= 2:
        variants.append(joined)
    variants.extend(item for item in normalized if len(item) >= 2)
    return variants


def _is_name_word(word: str) -> bool:
    """Tell a name-bearing word from the incidental capitals of a question."""

    if len(word) < 2 or word.isdigit() or word.lower() in _NON_NAME_WORDS:
        return False
    return any(character.isupper() for character in word) or word[:1].isdigit()


def _normalize_name(text: str) -> str:
    return _WORD_CHARACTERS_PATTERN.sub("", text.lower())


def _heading_terms(text: str) -> frozenset[str]:
    """Return the terms of every heading-like line of one page."""

    terms: set[str] = set()
    for line in text.splitlines():
        candidate = line.strip()
        if not 3 <= len(candidate) <= 90:
            continue
        letters = [character for character in candidate if character.isalpha()]
        if not letters:
            continue
        words = candidate.split()
        upper_share = sum(1 for character in letters if character.isupper()) / len(letters)
        title_case = len(words) <= 9 and all(word[:1].isupper() for word in words if word[:1].isalpha())
        if upper_share >= 0.6 or title_case or _STRUCTURAL_HEADING.match(candidate):
            terms.update(_TERM_PATTERN.findall(candidate.lower()))
    return frozenset(terms)


def _is_statement_page(text: str) -> bool:
    """Tell a page that presents a primary financial statement from a narrative page."""

    head = _WHITESPACE_PATTERN.sub("", text[:_STATEMENT_HEAD_CHARS].lower())
    return any(marker in head for marker in _STATEMENT_MARKERS)


def _page_body(document_id: str, text: str) -> str:
    """Drop the document line of the projected content, keeping the page body."""

    prefix = f"{document_id}\n"
    return text[len(prefix) :] if text.startswith(prefix) else text


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
        raise ImportError("PDF parsing requires the pypdf package") from error  # noqa: TRY003

    try:
        reader = PdfReader(io.BytesIO(content))
        encrypted = reader.is_encrypted
    except Exception as error:
        raise ValueError("PDF content could not be parsed completely") from error  # noqa: TRY003

    if encrypted and not reader.decrypt(""):
        raise ValueError("PDF content is encrypted with an unknown password")  # noqa: TRY003
    try:
        return tuple(_normalize_text(page.extract_text() or "") for page in reader.pages)
    except Exception as error:
        raise ValueError("PDF content could not be parsed completely") from error  # noqa: TRY003


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
    while len(f"{document_id}\n{truncated}".encode()) > MAX_PDF_PAGE_TEXT_BYTES:
        truncated = truncated[:-1]
    return truncated


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )
