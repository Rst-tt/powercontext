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
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryEntryInput, MemoryService
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft

REVISION = 5


def _config(tmp_path: Path) -> BuiltinConfig:
    return BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}"))


def _event(node_id: str, sequence: int, *, available: bool, revision: int = REVISION) -> SourceLifecycleEvent:
    return SourceLifecycleEvent(
        node_id=node_id,
        revision=revision,
        sequence=sequence,
        available=available,
        reason="operational change",
    )


async def _scope(runtime: Any, *, key: str) -> str:
    scope = await runtime.scopes.create(
        ScopeDraft(title="Lifecycle", summary="Dependency evidence", idempotency_key=key)
    )
    return scope.scope_id


async def _remember(runtime: Any, scope_id: str, text: str) -> Any:
    result = await runtime.memory.for_scope(scope_id).remember(
        RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=text),))
    )
    return result.entry.citation


async def _topic(runtime: Any, scope_id: str, node: str) -> ArtifactRef:
    created = await runtime.records.for_scope(scope_id).create_artifact(
        "topic-memory",
        ArtifactWrite(content={"title": node, "summary": f"{node} summary", "detail": f"{node} detail"}),
    )
    return ArtifactRef(family="topic-memory", artifact_id=created.artifact_id, revision=created.revision)


async def _memory_state(runtime: Any, scope_id: str, node: str) -> str:
    page = await runtime.memory.for_scope(scope_id).list(include_inactive=True)
    states = {record.entry.text.split()[0]: record.state for record in page.entries}
    return states[node]


async def _memory_searchable(runtime: Any, scope_id: str, node: str) -> bool:
    page = await runtime.memory.for_scope(scope_id).search(SearchMemoryRequest(query=node, mode="fts", limit=10))
    return any(hit.text == f"{node} evidence" for hit in page.hits)


async def _topic_searchable(runtime: Any, scope_id: str, node: str) -> bool:
    page = await runtime.topic_memory.for_scope(scope_id).search(SearchTopicMemoryRequest(query=node, limit=20))
    return bool(page.hits)


async def _build(runtime: Any, scope_id: str) -> tuple[Any, dict[str, Any]]:
    """Build one retired Source, one surviving Source, and their derived evidence."""

    lifecycle = runtime.lifecycle.for_scope(scope_id)
    references: dict[str, Any] = {}
    for node in ("retired", "kept"):
        receipt = await runtime.sources.for_scope(scope_id).capture(
            CaptureSource(source_id=node, content=f"Origin {node}", metadata={})
        )
        references[node] = receipt.source_ref
        await lifecycle.register_source(node, receipt.source_ref, revision=REVISION)
    references["sharedfact"] = await _remember(runtime, scope_id, "sharedfact evidence")
    await lifecycle.register_memory(
        "sharedfact", references["sharedfact"], parents=("retired", "kept"), policy="any"
    )
    references["solefact"] = await _remember(runtime, scope_id, "solefact evidence")
    await lifecycle.register_memory("solefact", references["solefact"], parents=("retired",), policy="all")
    references["digest"] = await _topic(runtime, scope_id, "digest")
    await lifecycle.register_topic("digest", references["digest"], parents=("solefact",), policy="all")
    return lifecycle, references


def test_withdrawal_and_restore_follow_all_and_any_support(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(_config(tmp_path)) as runtime:
            scope_id = await _scope(runtime, key="propagation")
            lifecycle, references = await _build(runtime, scope_id)

            assert await lifecycle.apply(_event("retired", 1, available=False)) == ["kept", "sharedfact"]
            assert await _memory_state(runtime, scope_id, "solefact") == "inactive"
            assert await _memory_searchable(runtime, scope_id, "solefact") is False
            assert await _memory_state(runtime, scope_id, "sharedfact") == "active"
            assert await _memory_searchable(runtime, scope_id, "sharedfact") is True
            assert await _topic_searchable(runtime, scope_id, "digest") is False

            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("solefact", purpose="generation")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("retired", purpose="generation")
            assert await lifecycle.resolve("solefact", purpose="audit") == references["solefact"]
            assert await lifecycle.resolve("digest", purpose="audit") == references["digest"]
            assert await lifecycle.resolve("kept", purpose="generation") == references["kept"]

            citation = references["solefact"]
            original = await runtime.memory.for_scope(scope_id).get(GetMemoryEntryRequest(citation=citation))
            assert original.entry.text == "solefact evidence"
            stored = await runtime.records.for_scope(scope_id).get_artifact_revision(
                "topic-memory", references["digest"].artifact_id, references["digest"].revision
            )
            assert stored.content["title"] == "digest"

            assert await lifecycle.apply(_event("retired", 2, available=True)) == [
                "digest",
                "kept",
                "retired",
                "sharedfact",
                "solefact",
            ]
            assert await _memory_state(runtime, scope_id, "solefact") == "active"
            assert await _topic_searchable(runtime, scope_id, "digest") is True
            assert await lifecycle.resolve("solefact", purpose="generation") == references["solefact"]
            assert await lifecycle.resolve("retired", purpose="generation") == references["retired"]

    asyncio.run(scenario())


def test_registered_state_persists_across_restart_without_new_memory_revisions(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(_config(tmp_path)) as runtime:
            scope_id = await _scope(runtime, key="restart")
            lifecycle, _ = await _build(runtime, scope_id)
            await lifecycle.apply(_event("retired", 1, available=False))

        async with open_builtin_runtime(_config(tmp_path)) as runtime:
            lifecycle = runtime.lifecycle.for_scope(scope_id)
            assert await _memory_state(runtime, scope_id, "solefact") == "inactive"
            before = (await runtime.memory.for_scope(scope_id).list()).memory_ref
            assert await lifecycle.apply(_event("retired", 1, available=False)) == ["kept", "sharedfact"]
            assert (await runtime.memory.for_scope(scope_id).list()).memory_ref == before

    asyncio.run(scenario())


def test_retry_after_an_interrupted_memory_write_completes_the_propagation(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(_config(tmp_path)) as runtime:
            scope_id = await _scope(runtime, key="interrupted")
            lifecycle, _ = await _build(runtime, scope_id)

            def interrupted(service: MemoryService, memory: Any, *, entries: Any, reason: str | None = None) -> Any:
                raise RuntimeError("memory backend unavailable")

            event = _event("retired", 1, available=False)
            with patch.object(MemoryService, "forget", interrupted), pytest.raises(RuntimeError):
                await lifecycle.apply(event)
            assert await _memory_state(runtime, scope_id, "solefact") == "active"

            assert await lifecycle.apply(event) == ["kept", "sharedfact"]
            assert await _memory_state(runtime, scope_id, "solefact") == "inactive"
            assert await _memory_searchable(runtime, scope_id, "solefact") is False

    asyncio.run(scenario())


def test_registration_and_event_identity_reject_conflicts(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(_config(tmp_path)) as runtime:
            scope_id = await _scope(runtime, key="identity")
            lifecycle, references = await _build(runtime, scope_id)
            citation = references["sharedfact"]

            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("loop", citation, parents=("loop",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("orphan", citation, parents=(), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("stray", citation, parents=("missing",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("twice", citation, parents=("kept", "kept"), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("sharedfact", citation, parents=("kept",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("oddpolicy", citation, parents=("kept",), policy="some")

            await lifecycle.register_memory("sharedfact", citation, parents=("retired", "kept"), policy="any")

            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(_event("missing", 1, available=False))
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(_event("retired", 1, available=False, revision=REVISION + 1))

            await lifecycle.apply(_event("retired", 3, available=False))
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(_event("retired", 2, available=True))
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(_event("retired", 3, available=True))
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(_event("sharedfact", 4, available=False))
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("sharedfact", purpose="prediction")

    asyncio.run(scenario())


def test_event_requires_a_complete_lifecycle_record() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEvent(
            node_id=" ",
            revision=0,
            sequence=0,
            available=False,
            reason="",
            unexpected=True,
        )
