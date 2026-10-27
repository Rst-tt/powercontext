from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactAddress, ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import (
    MemoryEntryAddress,
    PreparedContextBuilder,
    PreparedMemoryCandidates,
)
from powercontext.builtin.runtime.prepared_code import CodeEvidenceRef, PreparedCodeCandidate
from powercontext.builtin.runtime.prepared_evidence import segment_sentences

MEMORY_REF = ArtifactRef(family="memory", artifact_id="memory", revision=3)
SCOPE = "scope-evidence"


def _hit(entry_id: str, text: str) -> MemoryHit:
    return MemoryHit(
        memory_ref=MEMORY_REF,
        entry_id=entry_id,
        entry_version_id=f"{entry_id}-v1",
        text=text,
        score=1.0,
        matched_by=("fts",),
    )


def _build(
    query: str,
    hits: tuple[MemoryHit, ...],
    *,
    max_bytes: int = 8000,
    selection: bool = True,
    scope_id: str = SCOPE,
    current_scope_id: str | None = SCOPE,
):
    return PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query=query, max_bytes=max_bytes, evidence_selection=selection),
        current_scope_id=current_scope_id,
        memory_candidates=(PreparedMemoryCandidates(scope_id=scope_id, memory_ref=MEMORY_REF, hits=hits),),
    )


def _items(content: str | None) -> list[dict[str, object]]:
    assert content is not None
    return json.loads(content.splitlines()[-2])["items"]


def test_query_selects_late_literal_evidence_with_exact_range_and_citation() -> None:
    prefix = "General administrative notes. " * 40
    late = "The cobalt sensor is calibrated at 37 degrees."
    text = prefix + late

    result = _build("cobalt sensor calibrated", (_hit("alpha", text),), max_bytes=1200)

    assert result.fragments
    fragment = next(fragment for fragment in result.fragments if "37" in fragment.text)
    assert fragment.text.endswith(late)
    assert fragment.end == len(text)
    assert text[fragment.start : fragment.end] == fragment.text
    assert fragment.address == MemoryEntryAddress(
        memory=ArtifactAddress(scope_id=SCOPE, artifact=MEMORY_REF),
        entry_id="alpha",
        entry_version_id="alpha-v1",
    )
    assert result.origins == tuple(item.address for item in result.fragments)

    items = _items(result.context.content)
    assert [item["content"] for item in items] == [item.text for item in result.fragments]
    assert [item["start"] for item in items] == [item.start for item in result.fragments]
    assert [item["end"] for item in items] == [item.end for item in result.fragments]
    assert items[0]["citation"] == {
        "scope_id": SCOPE,
        "memory_ref": MEMORY_REF.model_dump(mode="json"),
        "entry_id": "alpha",
        "entry_version_id": "alpha-v1",
    }
    assert result.context.content_bytes == len(result.context.content.encode("utf-8")) <= 1200


def test_different_query_needs_different_evidence() -> None:
    hits = (
        _hit("alpha", "Opening remarks. The cobalt sensor is calibrated at 37 degrees. Closing remarks."),
        _hit("beta", "Opening remarks. The amber valve opens at 19 degrees. Closing remarks."),
    )

    cobalt = _build("cobalt sensor calibrated", hits, max_bytes=1200)
    amber = _build("amber valve opens", hits, max_bytes=1200)

    assert cobalt.fragments[0].address.entry_id == "alpha"
    assert "37" in cobalt.fragments[0].text
    assert amber.fragments[0].address.entry_id == "beta"
    assert "19" in amber.fragments[0].text


def test_adjacent_selected_sentences_share_one_fragment() -> None:
    text = (
        "The cobalt sensor is calibrated at 37 degrees. "
        "It reports the calibrated value every minute. "
        "Unrelated notes about the shipping schedule follow here."
    )

    result = _build("cobalt sensor calibrated", (_hit("alpha", text),), max_bytes=1200)

    merged = [fragment for fragment in result.fragments if "37 degrees" in fragment.text]
    assert len(merged) == 1
    assert "every minute" in merged[0].text
    assert text[merged[0].start : merged[0].end] == merged[0].text


def test_default_off_keeps_ordinary_prepare_behaviour() -> None:
    text = "A station records temperature. " * 20

    result = _build("temperature", (_hit("alpha", text),), max_bytes=1200, selection=False)

    assert result.fragments == ()
    items = _items(result.context.content)
    assert items[0]["content"] == text
    assert items[0]["truncated"] is False
    assert result.context.content_bytes == len(result.context.content.encode("utf-8")) <= 1200


def test_utf8_budget_bounds_unicode_evidence() -> None:
    text = "海洋观测 station records temperatures. " * 40

    result = _build("海洋观测 station", (_hit("alpha", text),), max_bytes=900)

    assert result.fragments
    fragment = result.fragments[0]
    assert text[fragment.start : fragment.end] == fragment.text
    assert result.context.content_bytes == len(result.context.content.encode("utf-8")) <= 900


def test_no_fragment_fits_returns_an_empty_context() -> None:
    text = "The cobalt sensor is calibrated at 37 degrees. " + "Additional detail follows here. " * 30

    result = _build("cobalt sensor calibrated", (_hit("alpha", text),), max_bytes=512)

    assert result.fragments == ()
    assert result.origins == ()
    assert result.context.status == "empty"
    assert result.context.content is None
    assert result.context.content_bytes == 0


def test_cross_scope_candidates_keep_their_own_scope_identity() -> None:
    text = "Opening remarks. The cobalt sensor is calibrated at 37 degrees."

    result = _build(
        "cobalt sensor calibrated",
        (_hit("alpha", text),),
        max_bytes=1200,
        scope_id="other-scope",
        current_scope_id="current-scope",
    )

    assert {fragment.address.memory.scope_id for fragment in result.fragments} == {"other-scope"}
    assert {item["citation"]["scope_id"] for item in _items(result.context.content)} == {"other-scope"}


def test_probe_budget_reports_the_evidence_selection() -> None:
    text = "Opening remarks. The cobalt sensor is calibrated at 37 degrees. " + "Filler detail. " * 200
    request = PrepareContextRequest(query="cobalt sensor calibrated", max_bytes=1200, evidence_selection=True)
    candidates = (PreparedMemoryCandidates(scope_id=SCOPE, memory_ref=MEMORY_REF, hits=(_hit("alpha", text),)),)

    built = PreparedContextBuilder().build_scopes_result(
        request=request,
        current_scope_id=SCOPE,
        memory_candidates=candidates,
    )
    probe = PreparedContextBuilder().probe_budget(
        request=request,
        current_scope_id=SCOPE,
        memory_candidates=candidates,
    )

    assert probe.max_bytes == 1200
    assert probe.delivered_items == len(built.fragments) == len(built.origins)
    assert probe.dropped_items > 0
    assert probe.unused_bytes == 1200 - built.context.content_bytes


def test_evidence_selection_is_a_strict_default_off_switch() -> None:
    assert PrepareContextRequest(query="query").evidence_selection is False
    with pytest.raises(ValidationError):
        PrepareContextRequest(query="query", evidence_selection="yes")


def test_sentence_spans_skip_abbreviations_decimals_and_lowercase_continuations() -> None:
    text = "See Fig. 3 for details. Alpha is 3.5 percent. U.S. samples were used. Done."

    assert [text[start:end] for start, end in segment_sentences(text)] == [
        "See Fig. 3 for details.",
        "Alpha is 3.5 percent.",
        "U.S. samples were used.",
        "Done.",
    ]
    continuation = "Measured twice. then verified."
    assert [continuation[start:end] for start, end in segment_sentences(continuation)] == [
        "Measured twice. then verified."
    ]


def test_missing_candidates_produce_an_empty_context() -> None:
    result = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt sensor calibrated", max_bytes=1200, evidence_selection=True),
        current_scope_id=SCOPE,
        memory_candidates=(),
    )

    assert result.fragments == ()
    assert result.origins == ()
    assert result.context.status == "empty"
    assert result.context.content_bytes == 0


def test_selection_mode_stays_inside_memory_candidates() -> None:
    text = "Opening remarks. The cobalt sensor is calibrated at 37 degrees."
    request = PrepareContextRequest(
        query="cobalt sensor calibrated",
        max_bytes=1200,
        evidence_selection=True,
        include_code=True,
    )
    code = PreparedCodeCandidate(
        origin=CodeEvidenceRef(
            scope_id=SCOPE,
            fingerprint="sha256:sensor",
            path="src/sensor.py",
            file_sha256="sha256:file",
            start_line=1,
            end_line=3,
            snippet_sha256="sha256:snippet",
        ),
        content="# calibration constants\nvalue = 37\n",
        checked_at="2026-01-01T00:00:00+00:00",
    )
    candidates = (PreparedMemoryCandidates(scope_id=SCOPE, memory_ref=MEMORY_REF, hits=(_hit("alpha", text),)),)

    built = PreparedContextBuilder().build_scopes_result(
        request=request,
        current_scope_id=SCOPE,
        memory_candidates=candidates,
        code_candidates=(code,),
    )

    assert built.code_origins == ()
    assert built.fragments
    assert all("calibration constants" not in fragment.text for fragment in built.fragments)
