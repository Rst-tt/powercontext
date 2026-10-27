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

"""seekdb Memory search index that answers both hybrid channels in one statement."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pyobvector import VECTOR
from sqlalchemy import Table, bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemoryCapabilities,
    MemoryProjection,
    MemorySearchChannels,
    MemorySearchRequest,
)
from powercontext.builtin.artifacts.memory.canonical import validate_embedding
from powercontext.builtin.persistence.memory_index import memory_channel_hits
from powercontext.builtin.persistence.oceanbase.memory_index import (
    OceanBaseMemoryFTSIndex,
    OceanBaseMemoryVectorIndex,
)
from powercontext.builtin.persistence.tags import memory_tag_parameters, memory_tag_sql

# seekdb plans each parenthesized branch separately, so this one statement keeps
# the fulltext scan and the approximate vector scan of the frozen two-statement
# path while removing the second round trip. The head alias is ``m`` in both
# branches so the shared native tag predicate applies unchanged.
_SEEKDB_HYBRID_SEARCH_SQL = """
(
SELECT
    'fts' AS channel,
    m.memory_artifact_id,
    m.entry_id,
    m.entry_version_id,
    v.text,
    MATCH (m.searchable_text) AGAINST (:analyzed_query) AS fts_score,
    CAST(NULL AS DOUBLE) AS distance
FROM pc_memory_entry_heads AS m
JOIN pc_memory_entry_versions AS v
  ON v.scope_id = m.scope_id
 AND v.memory_artifact_id = m.memory_artifact_id
 AND v.entry_version_id = m.entry_version_id
WHERE m.scope_id = :scope_id
  AND m.memory_artifact_id IN :memory_artifact_ids
  AND MATCH (m.searchable_text) AGAINST (:analyzed_query)
{tag}
ORDER BY fts_score DESC, m.memory_artifact_id, m.entry_id, m.entry_version_id
LIMIT :candidate_limit
)
UNION ALL
(
SELECT
    'vector' AS channel,
    m.memory_artifact_id,
    m.entry_id,
    m.entry_version_id,
    v.text,
    CAST(NULL AS DOUBLE) AS fts_score,
    l2_distance(m.embedding, :query_vector) AS distance
FROM pc_memory_vector_entries AS m
JOIN pc_memory_entry_versions AS v
  ON v.scope_id = m.scope_id
 AND v.memory_artifact_id = m.memory_artifact_id
 AND v.entry_version_id = m.entry_version_id
WHERE m.scope_id = :scope_id
  AND m.memory_artifact_id IN :memory_artifact_ids
{tag}
ORDER BY l2_distance(m.embedding, :query_vector) APPROXIMATE
LIMIT :candidate_limit
)
"""


class SeekDBMemoryHybridIndex:
    """Both Memory search channels as one seekdb candidate statement.

    ``mode="hybrid"`` is the only mode this index rewrites. Raising the index
    count would cost a second round trip, so the branches share a statement and
    the caller still receives two ranked channels. Single-channel requests keep
    delegating to the plain FULLTEXT and HNSW indexes unchanged.
    """

    def __init__(self, profile: EmbeddingProfile) -> None:
        self.profile = profile
        self.fts = OceanBaseMemoryFTSIndex()
        self.vector = OceanBaseMemoryVectorIndex(profile)
        self.capabilities = MemoryCapabilities(
            fts=True,
            vector=True,
            hybrid=True,
            tag_filter=True,
            embedding_profile=profile,
        )
        self.tables: tuple[Table, ...] = self.fts.tables + self.vector.tables
        self._search_sql = _hybrid_statement(tagged=False).bindparams(
            bindparam("memory_artifact_ids", expanding=True),
            bindparam("query_vector", type_=VECTOR(profile.dimension)),
        )
        self._tagged_search_sql = _hybrid_statement(tagged=True).bindparams(
            bindparam("memory_artifact_ids", expanding=True),
            bindparam("tag_keys", expanding=True),
            bindparam("tag_hashes", expanding=True),
            bindparam("query_vector", type_=VECTOR(profile.dimension)),
        )

    async def initialize(self, connection: AsyncConnection, /) -> None:
        await self.fts.initialize(connection)
        await self.vector.initialize(connection)

    async def replace(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        await self.fts.replace(connection, scope_id, memory_ref, projections)
        await self.vector.replace(connection, scope_id, memory_ref, projections)

    async def delete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        entry_ids: tuple[str, ...],
        /,
    ) -> None:
        await self.fts.delete(connection, scope_id, memory_ref, entry_ids)
        await self.vector.delete(connection, scope_id, memory_ref, entry_ids)

    async def upsert(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        await self.fts.upsert(connection, scope_id, memory_ref, projections)
        await self.vector.upsert(connection, scope_id, memory_ref, projections)

    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode == "fts":
            return await self.fts.search(connection, scope_id, request)
        if request.mode != "hybrid":
            return await self.vector.search(connection, scope_id, request)
        if request.embedding_profile != self.profile or request.query_vector is None:
            raise CapabilityNotSupportedError("embedding-profile")
        if not await self.vector.vector_complete(connection, scope_id, request.memories, self.profile):
            raise CapabilityNotSupportedError("vector")
        rows = (
            (
                await connection.execute(
                    self._search_sql if request.tag_filter is None else self._tagged_search_sql,
                    {
                        "scope_id": scope_id,
                        "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
                        "analyzed_query": request.analyzed_query,
                        "query_vector": validate_embedding(
                            request.query_vector,
                            dimension=self.profile.dimension,
                        ),
                        "candidate_limit": request.candidate_limit,
                        **memory_tag_parameters(request.tag_filter),
                    },
                )
            )
            .mappings()
            .all()
        )
        fts_rows = [row for row in rows if str(row["channel"]) == "fts"]
        vector_rows = [row for row in rows if str(row["channel"]) != "fts"]
        return MemorySearchChannels(
            # UNION ALL does not order the combined result, so each channel is
            # re-established from the score the statement computed.
            fts=memory_channel_hits(sorted(fts_rows, key=self._fts_key), request.memories),
            vector=memory_channel_hits(sorted(vector_rows, key=self._vector_key), request.memories),
        )

    async def vector_complete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memories: tuple[ArtifactRef, ...],
        profile: EmbeddingProfile,
        /,
    ) -> bool:
        return await self.vector.vector_complete(connection, scope_id, memories, profile)

    async def hydrate(
        self,
        connection: AsyncConnection,
        scope_id: str,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> tuple[MemoryProjection, ...]:
        return await self.vector.hydrate(connection, scope_id, projections)

    @staticmethod
    def _fts_key(row: Mapping[Any, Any]) -> tuple[Any, ...]:
        return (
            -float(row["fts_score"]),
            str(row["memory_artifact_id"]),
            str(row["entry_id"]),
            str(row["entry_version_id"]),
        )

    @staticmethod
    def _vector_key(row: Mapping[Any, Any]) -> tuple[Any, ...]:
        return (
            float(row["distance"]),
            str(row["memory_artifact_id"]),
            str(row["entry_id"]),
            str(row["entry_version_id"]),
        )


def _hybrid_statement(*, tagged: bool) -> Any:
    return text(_SEEKDB_HYBRID_SEARCH_SQL.replace("{tag}", memory_tag_sql("m") if tagged else ""))
