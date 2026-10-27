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

"""Durable Source lifecycle bindings, their availability, and their edges."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.codec import dump_model, load_model
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.errors import InvalidRepositoryArgumentError
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE
from powercontext.builtin.sources import validate_scope_id
from powercontext.limits import MAX_SOURCE_LIFECYCLE_NODE_ID_LENGTH

SOURCE_LIFECYCLE_KIND_SOURCE = "source"
SOURCE_LIFECYCLE_KIND_MEMORY = "memory"
SOURCE_LIFECYCLE_KIND_TOPIC = "topic"
SOURCE_LIFECYCLE_KINDS = (SOURCE_LIFECYCLE_KIND_SOURCE, SOURCE_LIFECYCLE_KIND_MEMORY, SOURCE_LIFECYCLE_KIND_TOPIC)

SOURCE_LIFECYCLE_POLICY_ALL = "all"
SOURCE_LIFECYCLE_POLICY_ANY = "any"
SOURCE_LIFECYCLE_POLICIES = (SOURCE_LIFECYCLE_POLICY_ALL, SOURCE_LIFECYCLE_POLICY_ANY)

_PARENTS_KIND = "source-lifecycle-parents"
_REFERENCE_KIND = "source-lifecycle-reference"


@dataclass(frozen=True, slots=True)
class SourceLifecycleNode:
    """One registered Source lifecycle node and its current availability."""

    node_id: str
    ordinal: int
    kind: str
    policy: str
    reference: bytes
    parents: tuple[str, ...]
    source_revision: int | None
    sequence: int | None
    event_digest: str | None
    artifact_id: str | None
    available: bool


def encode_parents(parents: tuple[str, ...]) -> bytes:
    """Encode the parent edges of one node as a canonical JSON array."""

    return json.dumps(list(parents), separators=(",", ":")).encode()


def decode_parents(payload: bytes) -> tuple[str, ...]:
    """Decode the parent edges of one node from its canonical JSON array."""

    try:
        decoded = json.loads(payload.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidRepositoryArgumentError("parents", "payload is not a JSON array") from error
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise InvalidRepositoryArgumentError("parents", "payload is not a JSON array of strings")
    return tuple(decoded)


def encode_reference(reference: Any, /, *, node_id: str) -> bytes:
    """Encode one binding reference as its canonical JSON payload."""

    return dump_model(reference, kind=_REFERENCE_KIND, name=node_id)


def decode_reference(model: type[Any], payload: bytes, /, *, node_id: str) -> Any:
    """Decode one binding reference from its canonical JSON payload."""

    return load_model(model, payload, kind=_REFERENCE_KIND, name=node_id)


class SourceLifecycleRepository:
    """Read and write the Source lifecycle nodes of one Scope."""

    async def list_nodes(self, connection: AsyncConnection, scope_id: str, /) -> tuple[SourceLifecycleNode, ...]:
        """Return every node registered in one Scope, in registration order."""

        scope = validate_scope_id(scope_id)
        rows = (
            (
                await connection.execute(
                    select(SOURCE_LIFECYCLE_NODES_TABLE)
                    .where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope)
                    .order_by(SOURCE_LIFECYCLE_NODES_TABLE.c.ordinal)
                )
            )
            .mappings()
            .all()
        )
        return tuple(_decode_node(row) for row in rows)

    async def save_nodes(
        self,
        connection: AsyncConnection,
        scope_id: str,
        nodes: tuple[SourceLifecycleNode, ...],
        /,
    ) -> None:
        """Insert or update the given nodes of one Scope."""

        scope = validate_scope_id(scope_id)
        for node in nodes:
            values = {
                "scope_id": scope,
                "node_id": node.node_id,
                "ordinal": node.ordinal,
                "kind": node.kind,
                "policy": node.policy,
                "reference": node.reference,
                "parents": encode_parents(node.parents),
                "source_revision": node.source_revision,
                "sequence": node.sequence,
                "event_digest": node.event_digest,
                "artifact_id": node.artifact_id,
                "available": 1 if node.available else 0,
            }
            await connection.execute(_upsert(connection, values))

    async def withdrawn_topic_artifact_ids(
        self,
        connection: AsyncConnection,
        scope_id: str,
        /,
    ) -> frozenset[str]:
        """Return the Topic Memory artifacts whose every binding is unavailable."""

        scope = validate_scope_id(scope_id)
        rows = (
            (
                await connection.execute(
                    select(
                        SOURCE_LIFECYCLE_NODES_TABLE.c.artifact_id,
                        SOURCE_LIFECYCLE_NODES_TABLE.c.available,
                    ).where(
                        SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope,
                        SOURCE_LIFECYCLE_NODES_TABLE.c.kind == SOURCE_LIFECYCLE_KIND_TOPIC,
                    )
                )
            )
            .mappings()
            .all()
        )
        supported: dict[str, bool] = {}
        for row in rows:
            artifact_id = row["artifact_id"]
            if artifact_id is None:
                continue
            supported[artifact_id] = supported.get(artifact_id, False) or bool(row["available"])
        return frozenset(artifact_id for artifact_id, available in supported.items() if not available)


def _upsert(connection: AsyncConnection, values: dict[str, Any]):
    """Build the dialect-specific upsert for one node row."""

    table = SOURCE_LIFECYCLE_NODES_TABLE
    updated = {name: value for name, value in values.items() if name not in ("scope_id", "node_id")}
    if connection.dialect.name == "sqlite":
        return sqlite_insert(table).values(**values).on_conflict_do_update(
            index_elements=["scope_id", "node_id"],
            set_=updated,
        )
    if connection.dialect.name == "mysql":
        return mysql_insert(table).values(**values).on_duplicate_key_update(**updated)
    raise InvalidRepositoryArgumentError("dialect", "Source lifecycle nodes require SQLite or MySQL")


def _decode_node(row: Any) -> SourceLifecycleNode:
    return SourceLifecycleNode(
        node_id=str(row["node_id"]),
        ordinal=int(row["ordinal"]),
        kind=str(row["kind"]),
        policy=str(row["policy"]),
        reference=bytes(row["reference"]),
        parents=decode_parents(bytes(row["parents"])),
        source_revision=None if row["source_revision"] is None else int(row["source_revision"]),
        sequence=None if row["sequence"] is None else int(row["sequence"]),
        event_digest=None if row["event_digest"] is None else str(row["event_digest"]),
        artifact_id=None if row["artifact_id"] is None else str(row["artifact_id"]),
        available=bool(row["available"]),
    )


def validate_node_id(node_id: str, /) -> str:
    """Return one node id that is safe to store as an identity column."""

    if not isinstance(node_id, str) or not node_id.strip():
        raise ValueError("node_id")
    if len(node_id) > MAX_SOURCE_LIFECYCLE_NODE_ID_LENGTH:
        raise ValueError("node_id")
    return node_id


class SourceLifecycleStore:
    """Own the transaction boundary for Source lifecycle persistence."""

    def __init__(self, database: AsyncDatabase, repository: SourceLifecycleRepository | None = None) -> None:
        self._database = database
        self._repository = SourceLifecycleRepository() if repository is None else repository

    async def list_nodes(self, scope_id: str, /) -> tuple[SourceLifecycleNode, ...]:
        """Return every node registered in one Scope, in registration order."""

        async with self._database.transaction() as connection:
            return await self._repository.list_nodes(connection, scope_id)

    async def save_nodes(self, scope_id: str, nodes: tuple[SourceLifecycleNode, ...], /) -> None:
        """Insert or update the given nodes of one Scope in one transaction."""

        async with self._database.transaction() as connection:
            await self._repository.save_nodes(connection, scope_id, nodes)

    async def withdrawn_topic_artifact_ids(self, scope_id: str, /) -> frozenset[str]:
        """Return the Topic Memory artifacts whose every binding is unavailable."""

        async with self._database.transaction() as connection:
            return await self._repository.withdrawn_topic_artifact_ids(connection, scope_id)


__all__ = (
    "SOURCE_LIFECYCLE_KINDS",
    "SOURCE_LIFECYCLE_KIND_MEMORY",
    "SOURCE_LIFECYCLE_KIND_SOURCE",
    "SOURCE_LIFECYCLE_KIND_TOPIC",
    "SOURCE_LIFECYCLE_POLICIES",
    "SOURCE_LIFECYCLE_POLICY_ALL",
    "SOURCE_LIFECYCLE_POLICY_ANY",
    "SourceLifecycleNode",
    "SourceLifecycleRepository",
    "SourceLifecycleStore",
    "decode_parents",
    "decode_reference",
    "encode_parents",
    "encode_reference",
    "validate_node_id",
)
