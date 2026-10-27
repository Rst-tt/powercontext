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

"""Pending Source counts that drive count-triggered Family admission."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.tables import SOURCE_JOURNAL_HEADS_TABLE


@dataclass(frozen=True, slots=True)
class PendingSourceCounts:
    """One page of Scope identifiers and the Source identities each still owes."""

    counts: tuple[tuple[str, int], ...]
    next_after: str | None


class SourceCountScanner:
    """Measure the unprocessed Source identities one Scope owes one Family.

    The count is the number of persisted journal identities after the Family
    cursor, so it never depends on Source length, token count, or generated
    result count. A replayed capture and a receipt trust upgrade keep their
    journal position, while a lineage-only Source still occupies one journal
    entry, so every journal identity contributes exactly one count.
    """

    def __init__(self, binding_name: str) -> None:
        self._binding_name = binding_name

    async def pending_counts(
        self,
        connection: AsyncConnection,
        after_scope_id: str | None,
        limit: int,
        /,
    ) -> PendingSourceCounts:
        """Return one page of Scope identifiers with their pending Source counts."""

        heads = SOURCE_JOURNAL_HEADS_TABLE
        statement = select(heads.c.scope_id, heads.c.position).order_by(heads.c.scope_id).limit(limit)
        if after_scope_id is not None:
            statement = statement.where(heads.c.scope_id > after_scope_id)
        rows = (await connection.execute(statement)).all()
        scope_ids = tuple(str(row[0]) for row in rows)
        cursors = await SourceCursorRepository().load_many(connection, scope_ids, self._binding_name)
        counts = tuple(
            (
                scope_id,
                int(position) - (cursors[scope_id].cursor.sequence if scope_id in cursors else 0),
            )
            for scope_id, position in zip(scope_ids, (int(row[1]) for row in rows), strict=True)
        )
        # A short page ends the sweep; a full page resumes after its last Scope.
        next_after = str(rows[-1][0]) if len(rows) == limit else None
        return PendingSourceCounts(counts=counts, next_after=next_after)
