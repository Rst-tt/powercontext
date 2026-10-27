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

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    PrepareContextRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.context_pack import (
    DEFAULT_CHUNK_BYTES,
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    aligned_pack,
    apply_context_pack_delta,
    prepared_pack,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import ContentSource

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"


class _ContentCandidatePipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(kind="fact", text=source.content, sources=(source,))
            for source in request.sources
            if isinstance(source, ContentSource)
        )


def _oracle_digest(items: tuple[bytes, ...]) -> str:
    payload = {"schema": PACK_SCHEMA, "items": [base64.b64encode(item).decode("ascii") for item in items]}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_pack_wire_bytes_carry_exactly_the_declared_fields() -> None:
    items = (b"first", "海".encode(), b"")
    pack = ContextPack.build(items)

    payload = json.loads(pack.to_bytes())
    assert set(payload) == {"schema", "items", "digest", "byte_count"}
    assert payload["schema"] == PACK_SCHEMA
    assert payload["digest"] == _oracle_digest(items)
    assert payload["byte_count"] == len(pack.to_bytes())
    assert pack.to_bytes() == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    assert ContextPack.from_bytes(pack.to_bytes()).items == items
    assert ContextPack.build(()).byte_count == len(ContextPack.build(()).to_bytes())


def test_from_content_splits_bounded_valid_utf8_and_keeps_the_body() -> None:
    content = "会议记录 Project Manager: remote review\n" * 40
    pack = ContextPack.from_content(content)

    assert b"".join(pack.items) == content.encode("utf-8")
    for item in pack.items:
        assert 0 < len(item) <= DEFAULT_CHUNK_BYTES
        item.decode("utf-8")
    assert all(len(item) <= 32 for item in ContextPack.from_content(content, chunk_bytes=32).items)

    assert ContextPack.from_content(None).items == ()
    assert ContextPack.from_content("").items == ()
    with pytest.raises(ContextPackError):
        ContextPack.from_content("海", chunk_bytes=2)


def test_delta_keeps_the_shared_prefix_and_rebuilds_from_wire_bytes_only() -> None:
    head = "PowerContext prepared untrusted historical context.\n\n" + "entry line\n" * 40
    base = prepared_pack(head)
    target = aligned_pack(base, head + "appended tail\n")
    delta = ContextPackDelta.between(base, target)

    assert delta.prefix_items > 0
    assert delta.appended == target.items[delta.prefix_items :]
    assert len(delta.to_bytes()) < len(target.to_bytes())

    rebuilt = apply_context_pack_delta(
        ContextPack.from_bytes(base.to_bytes()),
        ContextPackDelta.from_bytes(delta.to_bytes()),
    )
    assert rebuilt.items == target.items
    assert rebuilt.digest == target.digest
    assert b"".join(rebuilt.items) == (head + "appended tail\n").encode("utf-8")


def test_rebuild_accepts_any_shared_prefix_and_rejects_broken_deltas() -> None:
    base = prepared_pack("shared head\n" + "body\n" * 30)
    target = prepared_pack("shared head\n" + "body\n" * 30 + "tail\n")
    delta = ContextPackDelta.between(base, target)

    for prefix_items in range(delta.prefix_items + 1):
        rebuilt = apply_context_pack_delta(
            base, ContextPackDelta(base.digest, target.digest, prefix_items, target.items[prefix_items:])
        )
        assert rebuilt.items == target.items

    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta("sha256:" + "0" * 64, target.digest, 0, target.items))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, target.digest, len(base.items) + 1, ()))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, target.digest, 0, (b"x",)))

    serialized = json.loads(ContextPackDelta.between(base, target).to_bytes())
    assert set(serialized) == {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
    assert serialized["schema"] == DELTA_SCHEMA
    for broken in (True, 1.5, -1, "1"):
        with pytest.raises(ContextPackError):
            ContextPackDelta.from_bytes(json.dumps({**serialized, "prefix_items": broken}).encode("utf-8"))
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps({**serialized, "extra": 1}).encode("utf-8"))


def test_prepare_incremental_sends_a_pack_then_a_delta_that_rebuilds_the_body() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(
            BuiltinConfig(database=SQLiteConfig()),
            candidate_pipeline=_ContentCandidatePipeline(),
        ) as runtime:
            scope = await runtime.scopes.create(
                ScopeDraft(title="Incremental Pack", summary="Incremental pack test", idempotency_key="context-pack")
            )
            sources = runtime.sources.for_scope(scope.scope_id)
            await sources.capture(
                CaptureSource(source_id="turn-1", content="Project Manager: ship the remote check.", metadata={})
            )
            await runtime.memory.for_scope(scope.scope_id).flush()
            context = runtime.context.for_scope(scope.scope_id)
            request = PrepareContextRequest(query="remote")

            expected = await context.prepare(request)
            assert expected.content is not None
            first = await context.prepare_incremental(request)
            assert isinstance(first, ContextPack)
            assert b"".join(first.items).decode("utf-8") == expected.content

            unchanged = await context.prepare_incremental(request, base_pack=first)
            assert isinstance(unchanged, ContextPackDelta)
            assert unchanged.appended == ()
            assert len(unchanged.to_bytes()) < len(first.to_bytes())
            assert apply_context_pack_delta(first, unchanged).items == first.items

            await sources.capture(
                CaptureSource(source_id="turn-2", content="Designer: the remote check is green.", metadata={})
            )
            await runtime.memory.for_scope(scope.scope_id).flush()
            after_append = await context.prepare(request)
            second = await context.prepare_incremental(request, base_pack=first)
            assert isinstance(second, ContextPackDelta)
            assert len(second.to_bytes()) == second.byte_count
            rebuilt = apply_context_pack_delta(
                ContextPack.from_bytes(first.to_bytes()), ContextPackDelta.from_bytes(second.to_bytes())
            )
            assert after_append.content is not None
            assert b"".join(rebuilt.items).decode("utf-8") == after_append.content

    asyncio.run(scenario())
