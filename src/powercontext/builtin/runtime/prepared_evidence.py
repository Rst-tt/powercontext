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

"""Select query-relevant literal evidence spans from recalled Memory candidates.

Selection never rewrites, translates, or summarises the source: every span is a half-open
character range of one candidate's own text, so a caller can verify it against the original
entry by position.

Sentences of the candidates are ranked by the share of the query's weighted terms they carry,
where a term is weighted by how few candidate documents use it. The highest ranked sentences are
admitted while the rendered context stays inside the caller's UTF-8 byte budget, and adjacent
admitted sentences of one entry merge into a single span so their citation overhead is paid once.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass, replace

_SENTENCE_END = re.compile(r"[.!?][\"')]*(?=\s|$)")
_TOKEN = re.compile(r"[^\W\d_]+|\d+")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_WORD_TAIL = re.compile(r"[A-Za-z0-9.]+$")

_ABBREVIATIONS = frozenset(
    {
        "al",
        "approx",
        "ca",
        "cf",
        "co",
        "dept",
        "dr",
        "eq",
        "eqs",
        "est",
        "etc",
        "fig",
        "figs",
        "inc",
        "inst",
        "jr",
        "ltd",
        "max",
        "min",
        "misc",
        "mr",
        "mrs",
        "ms",
        "no",
        "nos",
        "pp",
        "prof",
        "ref",
        "refs",
        "reg",
        "resp",
        "sr",
        "st",
        "tab",
        "tabs",
        "univ",
        "vol",
        "vs",
    }
)
_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "against",
        "all",
        "also",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "during",
        "each",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "here",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "may",
        "might",
        "more",
        "most",
        "must",
        "no",
        "nor",
        "not",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "own",
        "same",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

_FRAGMENT_LIMIT = 8
_MORPHOLOGICAL_CREDIT = 0.5
_MORPHOLOGICAL_MIN_LENGTH = 6
_MIN_STEM_LENGTH = 4
_STRIP_SUFFIX_MIN_LENGTH = 7


@dataclass(frozen=True)
class EvidenceDocument:
    """One candidate entry whose text can contribute literal evidence."""

    index: int
    text: str


@dataclass(frozen=True)
class EvidenceUnit:
    """One ranked sentence of one document."""

    document: int
    index: int
    start: int
    end: int
    score: float
    density: float
    terms: frozenset[str]


@dataclass(frozen=True)
class EvidenceSpan:
    """One literal span of admitted sentences merged from one document."""

    document: int
    start: int
    end: int
    score: float
    unit: int
    units: int


def segment_sentences(text: str) -> tuple[tuple[int, int], ...]:
    """Split ``text`` into whitespace-trimmed sentence spans in reading order."""
    spans: list[tuple[int, int]] = []
    pending = 0
    for match in _SENTENCE_END.finditer(text):
        if not _ends_sentence(text, match):
            continue
        span = _trim(text, pending, match.end())
        if span is not None:
            spans.append(span)
        pending = match.end()
    span = _trim(text, pending, len(text))
    if span is not None:
        spans.append(span)
    return tuple(spans)


def select_evidence(
    *,
    query: str,
    documents: Sequence[EvidenceDocument],
    measure: Callable[[Sequence[EvidenceSpan]], int],
    max_bytes: int,
    fragment_limit: int = _FRAGMENT_LIMIT,
) -> tuple[EvidenceSpan, ...]:
    """Return the literal spans of ``documents`` that best answer ``query`` within ``max_bytes``.

    ``measure`` reports the rendered UTF-8 byte size of a candidate set, so the budget always
    covers the citation and envelope overhead and not just the selected text.
    """
    ranked = _rank_units(query=query, documents=documents)
    if not ranked:
        return ()
    matched = tuple(unit for unit in ranked if unit.score > 0.0)
    selected = _fit(matched, measure=measure, max_bytes=max_bytes, fragment_limit=fragment_limit)
    if not selected:
        # No sentence shares a term with the query. Literal context is still more useful than
        # nothing, so keep the leading sentences of the candidates instead.
        return _spans(_fit(ranked, measure=measure, max_bytes=max_bytes, fragment_limit=fragment_limit))
    fillers = _ordered_fillers(ranked, selected)
    selected = _fit(fillers, measure=measure, max_bytes=max_bytes, fragment_limit=fragment_limit, selected=selected)
    return _spans(selected)


def _ordered_fillers(ranked: Sequence[EvidenceUnit], selected: Sequence[EvidenceUnit]) -> tuple[EvidenceUnit, ...]:
    """Order the sentences that shared no term with the query for the leftover budget.

    Expanding a document that already answered the query keeps the admitted evidence readable
    and contiguous, and the merge of neighbouring sentences pays its citation only once. The
    document with the strongest admitted sentence is expanded first.
    """
    anchors: dict[int, tuple[float, tuple[int, ...]]] = {}
    for unit in selected:
        best, indexes = anchors.get(unit.document, (0.0, ()))
        anchors[unit.document] = (max(best, unit.score), (*indexes, unit.index))
    fillers = [unit for unit in ranked if unit.score <= 0.0 and unit.document in anchors]
    return tuple(
        sorted(
            fillers,
            key=lambda unit: (
                -anchors[unit.document][0],
                min(abs(unit.index - index) for index in anchors[unit.document][1]),
                unit.document,
                unit.index,
            ),
        )
    )


def _rank_units(*, query: str, documents: Sequence[EvidenceDocument]) -> tuple[EvidenceUnit, ...]:
    units = [
        EvidenceUnit(
            document=document.index,
            index=index,
            start=start,
            end=end,
            score=0.0,
            density=0.0,
            terms=_terms(document.text[start:end]),
        )
        for document in documents
        for index, (start, end) in enumerate(segment_sentences(document.text))
    ]
    if not units:
        return ()
    weights = _query_weights(query_terms=_query_terms(query), units=units, document_count=len(documents))
    total = math.fsum(weights.values()) or 1.0
    scored = tuple(_score_unit(unit, weights, total) for unit in units)
    return tuple(sorted(scored, key=_rank_key))


def _score_unit(unit: EvidenceUnit, weights: Mapping[str, float], total: float) -> EvidenceUnit:
    """Score a sentence by the share of the query's weighted terms that it carries."""
    matched = weights.keys() & unit.terms
    score = math.fsum(weights[term] for term in matched) / total
    score += _morphological_score(weights.keys() - matched, unit.terms, weights, total)
    length = unit.end - unit.start
    density = score / length if length > 0 else 0.0
    return replace(unit, score=score, density=density)


def _rank_key(unit: EvidenceUnit) -> tuple[float, float, int, int]:
    return (-unit.score, -unit.density, unit.document, unit.index)


def _morphological_score(
    unmatched: Set[str],
    terms: frozenset[str],
    weights: Mapping[str, float],
    total: float,
) -> float:
    """Credit a query term whose only candidate match is a longer or shorter word form."""
    score = 0.0
    for query_term in unmatched:
        if len(query_term) < _MORPHOLOGICAL_MIN_LENGTH:
            continue
        credit = _MORPHOLOGICAL_CREDIT * weights.get(query_term, 0.0) / total
        for term in terms:
            if len(term) < _MORPHOLOGICAL_MIN_LENGTH - 1:
                continue
            if query_term.startswith(term) or term.startswith(query_term):
                score += credit
                break
    return score


def _fit(
    plan: Sequence[EvidenceUnit],
    *,
    measure: Callable[[Sequence[EvidenceSpan]], int],
    max_bytes: int,
    fragment_limit: int,
    selected: Sequence[EvidenceUnit] = (),
) -> tuple[EvidenceUnit, ...]:
    """Admit sentences in rank order while the rendered context stays inside the budget."""
    chosen = tuple(selected)
    pending = tuple(plan)
    while pending:
        remaining: list[EvidenceUnit] = []
        accepted = False
        for unit in pending:
            trial = (*chosen, unit)
            spans = _spans(trial)
            if len(spans) <= fragment_limit and measure(spans) <= max_bytes:
                chosen = trial
                accepted = True
            else:
                remaining.append(unit)
        if not accepted:
            break
        pending = tuple(remaining)
    return chosen


def _spans(units: Sequence[EvidenceUnit]) -> tuple[EvidenceSpan, ...]:
    """Merge admitted sentences into one literal span per contiguous run of one document."""
    grouped: dict[int, list[EvidenceUnit]] = {}
    for unit in units:
        grouped.setdefault(unit.document, []).append(unit)
    spans: list[EvidenceSpan] = []
    for document in sorted(grouped):
        ordered = sorted(grouped[document], key=_unit_index)
        run = [ordered[0]]
        for unit in ordered[1:]:
            if unit.index == run[-1].index + 1:
                run.append(unit)
                continue
            spans.append(_merged(run))
            run = [unit]
        spans.append(_merged(run))
    return tuple(spans)


def _unit_index(unit: EvidenceUnit) -> int:
    return unit.index


def _merged(run: Sequence[EvidenceUnit]) -> EvidenceSpan:
    return EvidenceSpan(
        document=run[0].document,
        start=run[0].start,
        end=run[-1].end,
        score=max(unit.score for unit in run),
        unit=run[0].index,
        units=len(run),
    )


def _ends_sentence(text: str, match: re.Match[str]) -> bool:
    """Reject punctuation that closes an abbreviation, an initial, or a decimal number."""
    tail = _WORD_TAIL.search(text, 0, match.start())
    if tail is not None:
        word = tail.group(0)
        if "." in word or word.isdigit() or word.lower() in _ABBREVIATIONS:
            return False
        if len(word) == 1 and word.isalpha():
            return False
    following = match.end()
    while following < len(text) and text[following].isspace():
        following += 1
    return following >= len(text) or not text[following].islower()


def _trim(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return None if start >= end else (start, end)


def _query_terms(query: str) -> frozenset[str]:
    return frozenset(term for term in _terms(query) if term not in _STOPWORDS)


def _query_weights(
    *,
    query_terms: frozenset[str],
    units: Sequence[EvidenceUnit],
    document_count: int,
) -> dict[str, float]:
    """Weight query terms by how few candidate documents use them.

    Sentence-level frequency is too noisy for this shape of input: the candidates are a handful of
    whole documents, so a boilerplate verb shared by every abstract would read as rare when it is not.
    """
    per_document: dict[int, set[str]] = {}
    for unit in units:
        per_document.setdefault(unit.document, set()).update(unit.terms)
    frequency: dict[str, int] = {}
    for terms in per_document.values():
        for term in terms & query_terms:
            frequency[term] = frequency.get(term, 0) + 1
    return {term: _idf(frequency, document_count, term) for term in frequency}


def _idf(frequency: Mapping[str, int], total: int, term: str) -> float:
    count = frequency.get(term, 0)
    return math.log(1.0 + (total - count + 0.5) / (count + 0.5))


def _terms(text: str) -> frozenset[str]:
    terms: set[str] = set()
    for match in _TOKEN.finditer(text):
        token = match.group(0)
        for segment, is_cjk in _segments(token):
            if is_cjk:
                terms.update(_cjk_terms(segment))
            else:
                terms.add(_stem(segment.lower()))
    return frozenset(terms)


def _segments(token: str) -> list[tuple[str, bool]]:
    segments: list[tuple[str, bool]] = []
    pending = 0
    for match in _CJK.finditer(token):
        if match.start() > pending:
            segments.append((token[pending : match.start()], False))
        segments.append((match.group(0), True))
        pending = match.end()
    if pending < len(token):
        segments.append((token[pending:], False))
    return segments


def _cjk_terms(segment: str) -> list[str]:
    """Index CJK runs by character unigram and bigram, mirroring the built-in Analyzer."""
    terms: list[str] = []
    for index in range(len(segment)):
        terms.append(segment[index])
        if index + 1 < len(segment):
            terms.append(segment[index : index + 2])
    return terms


def _stem(term: str) -> str:
    """Collapse the light English inflections that separate a query word from its evidence."""
    if len(term) <= _MIN_STEM_LENGTH or not term.isalpha():
        return term
    if term.endswith("ies") and len(term) - 3 >= _MIN_STEM_LENGTH:
        return f"{term[:-3]}y"
    if term.endswith(("sses", "ches", "shes", "xes", "zes")):
        return term[:-2]
    if len(term) >= _STRIP_SUFFIX_MIN_LENGTH:
        for suffix in ("ing", "ed"):
            if term.endswith(suffix):
                return term[: -len(suffix)]
    if term.endswith("s") and not term.endswith(("ss", "us", "is")):
        return term[:-1]
    return term
