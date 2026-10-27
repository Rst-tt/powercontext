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

"""Capture the selected Pi session branch as immutable raw evidence events."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final, NoReturn
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.definitions import SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization

PI_EVENT_SOURCE_NAME: Final = "pi-event"
PI_SESSION_CONNECTOR_NAME: Final = "pi-session-history"
PI_SOURCE_VERSION: Final = "1"
PI_SESSION_VERSION: Final = 3
PI_CHECKPOINT_VERSION: Final = 1

__all__ = [
    "PI_CHECKPOINT_VERSION",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_VERSION",
    "PI_SOURCE_VERSION",
    "PiEventInput",
    "PiEventSource",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]


def _reject(message: str, cause: BaseException | None = None) -> NoReturn:
    """Reject one malformed Pi session input without partial results."""

    error = ValueError(message)
    if cause is None:
        raise error
    raise error from cause


class PiSessionInput(BaseModel):
    """Complete UTF-8 JSONL bytes plus the optional selected leaf."""

    model_config = ConfigDict(frozen=True)

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible Pi event on the selected ancestor chain."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """One captured Pi event addressed by its session and event identity."""

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_identity(self) -> PiEventSource:
        if self.name != pi_event_source_id(self.session_id, self.event_id):
            _reject("Pi event Source must use its session and event identity for name")
        return self


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build a stable, injective and bounded identity for one session event."""

    identity = f"{PI_EVENT_SOURCE_NAME}:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(identity) <= MAX_SOURCE_ID_LENGTH:
        return identity
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{PI_EVENT_SOURCE_NAME}=sha256:{digest}"


def parse_pi_session(value: PiSessionInput, /) -> tuple[PiEventInput, ...]:
    """Parse the complete selected branch of one Pi v3 session."""

    session_id, records = _read_graph(value.content)
    return _branch_events(session_id, records, value.leaf_id, _event_index(session_id, records))


class PiEventTextEvidenceProjection:
    """Expose one complete visible event as canonical textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class PiEventSourceAdapter:
    """Resolve and read one Pi event without interpreting its recorded content."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            name=pi_event_source_id(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    PiEventSourceAdapter(),
    version=PI_SOURCE_VERSION,
    projections=(PiEventTextEvidenceProjection(),),
)


class PiSessionConnector:
    """Import the visible events of one selected Pi session branch."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SOURCE_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        session_id, records = _read_graph(self._path.read_bytes())
        index = _event_index(session_id, records)
        events = _branch_events(session_id, records, self._leaf_id, index)
        imported = _load_checkpoint(session.checkpoint)
        recorded = {source_id: _record_digest(event.record) for source_id, event in index.items()}
        for source_id, digest in imported.items():
            current = recorded.get(source_id)
            if current is not None and current != digest:
                _reject(f"Pi session event {source_id!r} changed after it was imported")

        pending = [event for event in events if pi_event_source_id(session_id, event.event_id) not in imported]
        for event in pending:
            await session.submit(_item_id(event), PI_EVENT_SOURCE_NAME, event)
        updated = dict(imported)
        for event in pending:
            updated[pi_event_source_id(session_id, event.event_id)] = _record_digest(event.record)
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"version": PI_CHECKPOINT_VERSION, "events": updated},
        )


def _item_id(event: PiEventInput) -> str:
    """Name one submitted item after its event, or after its identity when unusable."""

    if event.event_id and event.event_id == event.event_id.strip():
        return event.event_id
    return pi_event_source_id(event.session_id, event.event_id)


def _read_graph(content: object) -> tuple[str, tuple[dict[str, Any], ...]]:
    """Decode and validate the whole session graph before selecting any branch."""

    lines = _decode_lines(content)
    if not lines:
        _reject("Pi session must start with a session header")
    return _header_session_id(lines[0]), _node_records(lines[1:])


def _decode_lines(content: object) -> list[str]:
    if not isinstance(content, bytes | bytearray | memoryview):
        _reject("Pi session content must be UTF-8 bytes")
    try:
        text = bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        _reject("Pi session content must be strict UTF-8", error)
    return [line for line in text.split("\n") if line.strip()]


def _record(line: str) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as error:
        _reject("Pi session records must be JSON objects", error)
    if not isinstance(value, dict):
        _reject("Pi session records must be JSON objects")
    return value


def _header_session_id(line: str) -> str:
    header = _record(line)
    if header.get("type") != "session":
        _reject("Pi session must start with a type=session record")
    version = header.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != PI_SESSION_VERSION:
        _reject("Pi session header must declare version 3")
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        _reject("Pi session header must declare a non-empty string id")
    return session_id


def _node_records(lines: list[str]) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    parents: dict[str, str | None] = {}
    for line in lines:
        record = _record(line)
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            _reject("Pi session entry must declare a non-empty string id")
        if event_id in parents:
            _reject(f"Pi session entry id {event_id!r} is duplicated")
        parent_id = record.get("parentId")
        if parent_id is not None:
            if not isinstance(parent_id, str) or not parent_id:
                _reject("Pi session entry parentId must be null or a non-empty string")
            if parent_id not in parents:
                _reject(f"Pi session entry {event_id!r} references an unknown parent {parent_id!r}")
        parents[event_id] = parent_id
        records.append(record)
    return tuple(records)


def _branch_events(
    session_id: str,
    records: tuple[dict[str, Any], ...],
    leaf_id: str | None,
    index: dict[str, PiEventInput],
) -> tuple[PiEventInput, ...]:
    if not records:
        _reject("Pi session has no branch entries")
    by_id = {record["id"]: record for record in records}
    current: str | None = records[-1]["id"] if leaf_id is None else leaf_id
    if not isinstance(current, str) or current not in by_id:
        _reject(f"Pi session leaf {current!r} is not an entry")

    chain: list[str] = []
    visited: set[str] = set()
    while current is not None:
        if current in visited:
            _reject("Pi session branch must not contain a parent cycle")
        visited.add(current)
        chain.append(current)
        current = by_id[current].get("parentId")
    chain.reverse()
    return tuple(
        index[identity] for event_id in chain if (identity := pi_event_source_id(session_id, event_id)) in index
    )


def _event_index(session_id: str, records: tuple[dict[str, Any], ...]) -> dict[str, PiEventInput]:
    """Index every visible event in the file by its durable Source identity."""

    index: dict[str, PiEventInput] = {}
    for record in records:
        event = _visible_event(session_id, record)
        if event is not None:
            index[pi_event_source_id(session_id, event.event_id)] = event
    return index


def _visible_event(session_id: str, record: dict[str, Any]) -> PiEventInput | None:
    """Project one entry's raw visible body, ignoring control entries."""

    event_type = record.get("type")
    if event_type == "compaction" or event_type == "branch_summary":
        role = event_type
        body = _summary_text(record.get("summary"))
    elif event_type == "custom_message":
        role = "custom"
        body = _content_text(record.get("content"))
    elif event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = _field_text(message.get("role"))
        body = _message_body(role, message)
    else:
        return None
    if not body.strip():
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=record["id"],
        parent_id=record.get("parentId"),
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _message_body(role: str, message: dict[str, Any]) -> str:
    if role == "bashExecution":
        command = _field_text(message.get("command"))
        output = _field_text(message.get("output"))
        if not command and not output:
            return ""
        lines = [f"$ {command}" if command else "$"]
        if output:
            lines.append(output)
        return "\n".join(lines)
    if role == "toolResult":
        tool_name = _identity_text(message.get("toolName"), "unknown")
        call_id = _identity_text(message.get("toolCallId"), "unknown")
        header = f"[tool_result {tool_name} {call_id}]"
        text = _content_text(message.get("content"))
        return f"{header} {text}" if text else header
    return _content_text(message.get("content"))


def _content_text(value: Any) -> str:
    """Join the visible text of string or content-block payloads."""

    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif kind == "toolCall":
            parts.append(_tool_call_text(block))
    return "\n".join(parts)


def _tool_call_text(block: dict[str, Any]) -> str:
    name = _identity_text(block.get("name"), "unknown")
    call_id = _identity_text(block.get("id"), "unknown")
    return f"[tool_call {name} {call_id}] {_compact_json(block.get('arguments'))}"


def _compact_json(value: Any) -> str:
    if value is None:
        return "{}"
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        _reject("Pi tool call arguments must be a JSON value", error)


def _summary_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _field_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _identity_text(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and value.strip() else fallback


def _record_digest(record: dict[str, JsonValue]) -> str:
    """Fingerprint the complete raw record for historical rewrite detection."""

    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _load_checkpoint(value: JsonValue | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _reject("Pi session checkpoint must be a JSON object")
    events = value.get("events")
    if value.get("version") != PI_CHECKPOINT_VERSION or not isinstance(events, dict):
        _reject("Pi session checkpoint must use the supported shape")
    imported: dict[str, str] = {}
    for source_id, digest in events.items():
        if not isinstance(source_id, str) or not isinstance(digest, str):
            _reject("Pi session checkpoint must map Source identities to record digests")
        imported[source_id] = digest
    return imported
