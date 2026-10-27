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

"""Behavior of the Pi session branch capture and its durable progress."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CommitConnectorCheckpoint,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    ConnectorBinding,
    SourceDefinitionRegistry,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.connectors import ConnectorLifecycle
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiSessionConnector,
    PiSessionInput,
    parse_pi_session,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

_HEADER = {"type": "session", "version": 3, "id": "session"}


def _session_bytes(records: list[dict[str, Any]], *, session_id: str = "session") -> bytes:
    header = {**_HEADER, "id": session_id}
    return "\n".join(json.dumps(record) for record in [header, *records]).encode()


def _parse(records: list[dict[str, Any]], leaf_id: str | None = None, *, session_id: str = "session"):
    return parse_pi_session(PiSessionInput(content=_session_bytes(records, session_id=session_id), leaf_id=leaf_id))


def test_selected_branch_keeps_raw_records_and_visible_bodies() -> None:
    records = [
        {"type": "message", "id": "a", "parentId": None, "message": {"role": "user", "content": "alpha"}},
        {"type": "message", "id": "b", "parentId": "a", "message": {"role": "assistant", "content": "beta"}},
        {"type": "compaction", "id": "c", "parentId": "a", "summary": "kept summary"},
        {
            "type": "message",
            "id": "d",
            "parentId": "c",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "toolCall", "name": "read", "id": "call-1", "arguments": {"b": 1, "a": "é"}},
                ],
            },
        },
        {
            "type": "message",
            "id": "e",
            "parentId": "d",
            "message": {
                "role": "toolResult",
                "toolName": "read",
                "toolCallId": "call-1",
                "content": [{"type": "text", "text": "file contents"}],
            },
        },
    ]

    events = _parse(records, "e")

    assert [event.event_id for event in events] == ["a", "c", "d", "e"]
    assert [event.content for event in events] == [
        "user: alpha",
        "compaction: kept summary",
        'assistant: [tool_call read call-1] {"a":"é","b":1}',
        "toolResult: [tool_result read call-1] file contents",
    ]
    assert [event.parent_id for event in events] == [None, "a", "c", "d"]
    assert events[3].record == records[4]
    assert [event.event_id for event in _parse(records)] == ["a", "c", "d", "e"]


def test_control_entries_keep_the_branch_connected_without_text() -> None:
    records = [
        {"type": "message", "id": "a", "parentId": None, "message": {"role": "user", "content": "alpha"}},
        {"type": "model_change", "id": "b", "parentId": "a"},
        {
            "type": "message",
            "id": "c",
            "parentId": "b",
            "message": {"role": "bashExecution", "command": "printf result", "output": "result", "content": []},
        },
        {"type": "custom_message", "id": "d", "parentId": "c", "content": [{"type": "text", "text": "Custom"}]},
        {"type": "branch_summary", "id": "e", "parentId": "d", "summary": "branch summary"},
    ]

    events = _parse(records)

    assert [event.event_id for event in events] == ["a", "c", "d", "e"]
    assert [event.content for event in events] == [
        "user: alpha",
        "bashExecution: $ printf result\nresult",
        "custom: Custom",
        "branch_summary: branch summary",
    ]
    assert events[1].parent_id == "b"


@pytest.mark.parametrize(
    "records,leaf",
    [
        ([{"type": "model_change", "id": "a", "parentId": "missing"}], "a"),
        ([{"type": "model_change", "id": "a", "parentId": "a"}], "a"),
        ([{"type": "model_change", "id": "a", "parentId": None}] * 2, "a"),
        ([{"type": "model_change", "id": "a", "parentId": None}], "absent"),
    ],
)
def test_broken_graphs_are_rejected(records: list[dict[str, Any]], leaf: str) -> None:
    with pytest.raises(ValueError):
        _parse(records, leaf)


@pytest.mark.parametrize(
    "raw",
    [b"", b"{}\n", b"[]\n", b"null\n", b"\xff", b'{"type":"session","version":2,"id":"x"}\n'],
)
def test_broken_archives_are_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=raw))


def test_event_identity_is_stable_and_bounded() -> None:
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))

    async def scenario() -> None:
        records = [
            {"type": "message", "id": "a", "parentId": None, "message": {"role": "user", "content": "alpha"}},
            {"type": "message", "id": "b", "parentId": "a", "message": {"role": "user", "content": "beta"}},
        ]
        events = _parse(records, session_id="session")
        first = await registry.resolve(events[0])
        again = await registry.resolve(events[0])
        other = await registry.resolve(events[1])
        assert first.name == again.name
        assert first.name != other.name
        assert len(first.name) <= 256
        assert await registry.read(first) == events[0]
        evidence = registry.project(first, TEXT_EVIDENCE_PROJECTION_KEY)
        assert evidence["source_type"] == "pi-event"
        assert evidence["source_id"] == first.name
        assert evidence["content"] == "user: alpha"
        assert evidence["metadata"]["session_id"] == "session"
        assert evidence["metadata"]["event_id"] == "a"
        assert evidence["metadata"]["parent_id"] is None
        assert evidence["metadata"]["event_type"] == "message"
        assert evidence["metadata"]["role"] == "user"

        long_records = [
            {
                "type": "message",
                "id": "e" * 400,
                "parentId": None,
                "message": {"role": "user", "content": "alpha"},
            }
        ]
        long_event = _parse(long_records, session_id="s" * 400)[0]
        long_source = await registry.resolve(long_event)
        assert len(long_source.name) <= 256

    asyncio.run(scenario())


class _RuntimeSink:
    def __init__(self, runtime, registry: SourceDefinitionRegistry) -> None:
        self._runtime = runtime
        self._registry = registry

    async def submit(self, binding, item_id, definition_name, value):
        source = await self._registry.resolve(value)
        receipt = await self._runtime.ingestion.submit(
            SubmitSourceObservation(
                scope_id=binding.scope_id,
                observation=project_source_for_transport(self._registry, source),
            )
        )
        return receipt.source_ref


class _RuntimeCheckpoints:
    def __init__(self, runtime) -> None:
        self._runtime = runtime

    async def load(self, binding):
        return (await self._runtime.ingestion.checkpoint(binding)).checkpoint

    async def save(self, binding, checkpoint, *, expected):
        await self._runtime.ingestion.commit(
            CommitConnectorCheckpoint(binding=binding, checkpoint=checkpoint, expected=expected)
        )


def _write_session(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_bytes(_session_bytes(records))


def _connector_run(directory: Path, path: Path, leaf: str | None):
    """Run one Connector against a fresh runtime over the same database."""

    async def scenario():
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{directory / 'connector.db'}"))
        async with open_builtin_runtime(config) as runtime:
            scope = await runtime.scopes.create(
                ScopeDraft(title="Source", summary="Source", idempotency_key="pi-session")
            )
            binding = ConnectorBinding(
                scope_id=scope.scope_id,
                binding_id="pi",
                connector_name="pi-session-history",
                connector_version="1",
            )
            await runtime.ingestion.register(manifest_for_definition(PI_EVENT_SOURCE_DEFINITION))
            result = await ConnectorLifecycle(
                sink=_RuntimeSink(runtime, registry), checkpoints=_RuntimeCheckpoints(runtime)
            ).run(PiSessionConnector(path, leaf), binding)
        async with open_builtin_contexts(config) as contexts:
            entries = await (await contexts.get(scope.scope_id)).sources.journal.entries()
        return result, entries

    return asyncio.run(scenario())


def test_connector_imports_new_events_only_and_keeps_other_branches(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    records = [
        {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "Shared"}},
        {
            "type": "message",
            "id": "left",
            "parentId": "root",
            "message": {"role": "assistant", "content": "Left"},
        },
        {"type": "model_change", "id": "control", "parentId": "root"},
        {
            "type": "message",
            "id": "right",
            "parentId": "control",
            "message": {"role": "assistant", "content": "Right"},
        },
    ]
    _write_session(path, records)

    first, entries = _connector_run(tmp_path, path, "left")
    assert [item.status for item in first.items] == ["accepted", "accepted"]
    assert len(entries) == 2

    second, entries = _connector_run(tmp_path, path, "right")
    assert len(second.items) == 1
    assert second.previous_checkpoint == first.committed_checkpoint
    assert len(entries) == 3

    replay, entries = _connector_run(tmp_path, path, "right")
    assert replay.items == ()
    assert len(entries) == 3

    records.append({
        "type": "message",
        "id": "tail",
        "parentId": "right",
        "message": {"role": "assistant", "content": "Later"},
    })
    _write_session(path, records)
    appended, entries = _connector_run(tmp_path, path, "tail")
    assert len(appended.items) == 1
    assert len(entries) == 4

    path.write_bytes(path.read_bytes() + b"{bad json}\n")
    with pytest.raises(ValueError):
        _connector_run(tmp_path, path, "tail")

    _write_session(path, records)
    repaired, entries = _connector_run(tmp_path, path, "tail")
    assert repaired.items == ()
    assert repaired.previous_checkpoint == appended.committed_checkpoint
    assert len(entries) == 4


def test_connector_rejects_rewritten_history(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    records = [{"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "Shared"}}]
    _write_session(path, records)
    imported, _ = _connector_run(tmp_path, path, None)
    assert len(imported.items) == 1

    records[0]["message"]["content"] = "Rewritten"
    _write_session(path, records)
    with pytest.raises(ValueError):
        _connector_run(tmp_path, path, None)

    records[0]["message"]["content"] = "Shared"
    _write_session(path, records)
    repaired, entries = _connector_run(tmp_path, path, None)
    assert repaired.items == ()
    assert repaired.previous_checkpoint == imported.committed_checkpoint
    assert len(entries) == 1
