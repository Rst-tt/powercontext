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

"""Behavior of captured PDF pages and of question retrieval over them."""

from __future__ import annotations

import asyncio
import hashlib
import io
from collections.abc import Sequence
from pathlib import Path

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    SourceDefinitionRegistry,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    parse_pdf_document,
    pdf_page_source_id,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

#: Parsing needs the locked PDF dependency; the rest of the suite runs without it.
pypdf = pytest.importorskip("pypdf")

_PAGE_TEXTS = [
    "Table of Contents\nItem 1. Business\nAlpha sells snacks in Brazil.",
    "",
    "Consolidated Statement of Cash Flows\nNet cash provided by operating activities 10,000",
    "Item 3. Legal Proceedings\nThe risk committee reported derivative exposures.",
]


def _pdf(pages: Sequence[str]) -> bytes:
    """Build a minimal text PDF: one content stream per physical page."""

    count = len(pages)
    page_ids = [4 + 2 * index for index in range(count)]
    content_ids = [5 + 2 * index for index in range(count)]
    font_id = 4 + 2 * count
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            "<< /Type /Pages /Kids [" + " ".join(f"{number} 0 R" for number in page_ids) + f"] /Count {count} >>"
        ).encode(),
        font_id: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, text in enumerate(pages):
        stream = f"BT /F1 11 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
        objects[page_ids[index]] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_ids[index]} 0 R >>"
        ).encode()
        objects[content_ids[index]] = (
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode()
    out += b"0000000000 65535 f \n"
    for number in range(1, size):
        out += f"{offsets.get(number, 0):010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _encrypted(content: bytes, *, user_password: str) -> bytes:
    reader = pypdf.PdfReader(io.BytesIO(content))
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password=user_password, owner_password="owner", algorithm="RC4-128")  # noqa: S106
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class _PagePipeline:
    """Turn one captured page into its projected text evidence."""

    async def extract(self, request, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(
                kind="source-evidence",
                text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"],
                sources=(source,),
            )
            for source in request.sources
            if source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"].strip()
        )


async def _drain(memory, pages: int = len(_PAGE_TEXTS)) -> None:
    for _ in range(pages + 2):
        if not (await memory.flush()).processed:
            return
    raise AssertionError("flush did not drain the captured pages")  # noqa: TRY003


async def _capture(directory: Path, pages: Sequence[str], *, document_id: str = "RPT") -> str:
    content = _pdf(pages)
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{directory / 'sources.db'}"))
    async with open_builtin_runtime(config) as runtime:
        scope = await runtime.scopes.create(ScopeDraft(title="Pages", summary="Pages", idempotency_key="pages"))
        await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
        for page in parse_pdf_document(PDFDocumentInput(document_id=document_id, content=content)):
            source = await registry.resolve(page)
            await runtime.ingestion.submit(
                SubmitSourceObservation(
                    scope_id=scope.scope_id,
                    observation=project_source_for_transport(registry, source),
                )
            )
    return scope.scope_id


def _page_number(citation_source_id: str) -> int:
    return int(citation_source_id.rsplit(":", 1)[1])


def test_every_physical_page_is_captured_in_order() -> None:
    content = _pdf(_PAGE_TEXTS)
    pages = parse_pdf_document(PDFDocumentInput(document_id="RPT", content=content))

    assert [page.page_number for page in pages] == [1, 2, 3, 4]
    assert [" ".join(page.text.split()) for page in pages] == [" ".join(text.split()) for text in _PAGE_TEXTS]
    assert {page.document_id for page in pages} == {"RPT"}
    assert {page.document_sha256 for page in pages} == {hashlib.sha256(content).hexdigest()}


@pytest.mark.parametrize(
    "content",
    [b"", b"revenue 2022", b"%PDF-1.4\n", _pdf(_PAGE_TEXTS)[:-40]],
    ids=["empty", "not-a-pdf", "header-only", "truncated"],
)
def test_unusable_content_fails_without_returning_a_page(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="RPT", content=content))


def test_blank_document_id_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="   ", content=_pdf(_PAGE_TEXTS)))


def test_pdf_that_needs_no_password_is_read_and_a_locked_one_is_refused() -> None:
    unlocked = parse_pdf_document(
        PDFDocumentInput(document_id="RPT", content=_encrypted(_pdf(_PAGE_TEXTS), user_password=""))
    )
    assert [page.text for page in unlocked] == list(_PAGE_TEXTS)

    with pytest.raises(ValueError):
        parse_pdf_document(
            PDFDocumentInput(document_id="RPT", content=_encrypted(_pdf(_PAGE_TEXTS), user_password="s"))  # noqa: S106
        )


def test_page_identity_is_stable_and_bound_to_the_revision() -> None:
    first = parse_pdf_document(PDFDocumentInput(document_id="RPT", content=_pdf(_PAGE_TEXTS)))
    again = parse_pdf_document(PDFDocumentInput(document_id="RPT", content=_pdf(_PAGE_TEXTS)))
    revised = parse_pdf_document(PDFDocumentInput(document_id="RPT", content=_pdf([*_PAGE_TEXTS, "Later note"])))

    def identity(page):
        return pdf_page_source_id(page.document_id, page.document_sha256, page.page_number)

    assert [identity(page) for page in first] == [identity(page) for page in again]
    assert identity(first[0]) != identity(revised[0])
    assert len({identity(page) for page in first}) == len(first)
    long_id = "x" * 4096
    assert len(pdf_page_source_id(long_id, first[0].document_sha256, 1)) <= 256


async def _resolved(pages):
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    return [await registry.resolve(page) for page in pages]


def test_definition_and_projection_name_each_page_exactly() -> None:
    assert PDF_PAGE_SOURCE_DEFINITION.name == "pdf-page"
    assert PDF_PAGE_SOURCE_DEFINITION.version == "1"

    pages = parse_pdf_document(PDFDocumentInput(document_id="RPT", content=_pdf(_PAGE_TEXTS)))
    sources = asyncio.run(_resolved(pages))
    observation = project_source_for_transport(SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,)), sources[2])
    projection = observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)

    assert sources[2].name == sources[2].source_id
    assert projection["source_type"] == "pdf-page"
    assert projection["source_id"] == sources[2].source_id
    assert projection["content"] == f"RPT\n{_PAGE_TEXTS[2]}"
    assert projection["metadata"] == {
        "document_id": "RPT",
        "document_sha256": pages[2].document_sha256,
        "page_number": 3,
    }


def test_search_returns_distinct_pages_that_read_back_from_memory(tmp_path: Path) -> None:
    async def scenario():
        scope_id = await _capture(tmp_path, _PAGE_TEXTS)
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'sources.db'}"))
        async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline()) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            await _drain(memory)
            citations = await search_pdf_pages(
                memory, "Which committee reported derivative exposures in the legal proceedings?", limit=3
            )
            assert len(citations) == 3
            pages = []
            texts = []
            for citation in citations:
                entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
                assert len(entry.sources) == 1
                assert entry.sources[0].source_type == "pdf-page"
                pages.append(_page_number(entry.sources[0].source_id))
                texts.append(entry.text)
            assert len(set(pages)) == len(pages)
            assert 4 in pages
            assert texts[0] == f"RPT\n{_PAGE_TEXTS[3]}"
            return await search_pdf_pages(memory, "", limit=3), await search_pdf_pages(memory, "risk", limit=0)

    empty_question, no_limit = asyncio.run(scenario())
    assert empty_question == ()
    assert no_limit == ()


#: The statement page reports cargo carried for airlines and firm orders, but only the prose
#: page says who places the orders and who leases the aircraft. A question that asks for that
#: discussion must not be answered by the table pricing the same nouns.
_GATE_PAGES = [
    *(
        f"Item 1A. Risk Factors\nGeneral economic conditions may affect our results in {2000 + index}."
        for index in range(12)
    ),
    "Consolidated Statements of Income\nCargo carried for airlines and firm orders is 10,000.",
    "Our customers place orders for aircraft delivery, airlines lease aircraft from us and suppliers deliver parts.",
]
_GATE_QUESTION = "Which customers place orders for aircraft delivery and which airlines lease aircraft from suppliers?"


def test_a_narrative_question_is_answered_by_the_page_that_discusses_it(tmp_path: Path) -> None:
    async def scenario():
        scope_id = await _capture(tmp_path, _GATE_PAGES, document_id="GATE")
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'sources.db'}"))
        async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline()) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            await _drain(memory, len(_GATE_PAGES))
            citations = await search_pdf_pages(memory, _GATE_QUESTION, limit=1)
            entry = (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry
            return _page_number(entry.sources[0].source_id)

    assert asyncio.run(scenario()) == len(_GATE_PAGES)


#: The paragraph that answers a question runs over a page break, so the neighbouring page
#: belongs to the same evidence block even when only one of the two matches the wording.
_WINDOW_PAGES = [
    "Table of Contents\nItem 1. Business",
    "Item 1. Business\nOur primary customers are commercial airlines and leasing companies.",
    "Our customers negotiate delivery schedules several years before delivery.",
    "Item 1A. Risk Factors\nThe market for commercial airlines is cyclical and seasonal.",
    "Item 8. Financial Statements\nLong-term debt maturing after 2027.",
]


def test_a_page_beside_the_matching_page_is_offered_as_the_same_block(tmp_path: Path) -> None:
    async def scenario():
        scope_id = await _capture(tmp_path, _WINDOW_PAGES, document_id="CUST")
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'sources.db'}"))
        async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline()) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            await _drain(memory, len(_WINDOW_PAGES))
            citations = await search_pdf_pages(memory, "Who are the primary customers of the company?", limit=2)
            pages = []
            for citation in citations:
                entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
                pages.append(_page_number(entry.sources[0].source_id))
            return pages

    assert asyncio.run(scenario()) == [2, 3]
