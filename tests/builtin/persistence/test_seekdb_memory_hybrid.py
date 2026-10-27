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

"""seekdb answers both Memory hybrid channels from one candidate statement."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import Engine, event

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("pylibseekdb") is None,
    reason="install powercontext[seekdb] for the real embedded backend",
)

PROFILE = EmbeddingProfile(
    profile_id="seekdb-hybrid-single-statement",
    model="deterministic",
    dimension=3,
    distance="l2",
    normalization="unit",
)


class _Embedding:
    """Alpha and everything else land on different axes so both channels admit the same entry."""

    profile = PROFILE

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=tuple((1.0, 0.0, 0.0) if "alpha" in text.casefold() else (0.0, 1.0, 0.0) for text in texts)
        )


@contextmanager
def _statements() -> Iterator[list[tuple[str, object]]]:
    statements: list[tuple[str, object]] = []

    def record(_connection, _cursor, statement: str, parameters: object, _context, _executemany) -> None:
        statements.append((statement, parameters))

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", record)


def test_seekdb_hybrid_search_uses_one_candidate_statement(tmp_path: Path) -> None:
    async def scenario() -> None:
        # Embedded seekdb keeps a Unix socket below its data path, which overruns
        # the 108-byte limit under pytest's deep tmp_path tree.
        database_path = Path(tempfile.mkdtemp(prefix="seekdb-hybrid-", dir=os.environ.get("TMPDIR"))) / "db"
        async with open_builtin_contexts(
            BuiltinConfig(database=SeekDBConfig(path=database_path)), embedding_model=_Embedding()
        ) as contexts:
            memory = (await contexts.get("project")).artifacts.memory
            head = await memory.remember(
                memory=None,
                entries=(
                    MemoryEntryInput(kind="fact", text="Alpha semantic record."),
                    MemoryEntryInput(kind="fact", text="Beta lexical record."),
                ),
                mode="append",
            )
            assert head is not None

            with _statements() as statements:
                result = await memory.search("alpha", memories=(head,), mode="hybrid", limit=1)

            candidates = [
                statement
                for statement in statements
                if "pc_memory_vector_entries" in statement[0] and "pc_memory_entry_heads" in statement[0]
            ]
            assert len(candidates) == 1
            statement, parameters = candidates[0]
            assert "AGAINST" in statement.upper()
            assert "l2_distance" in statement.lower()

            assert [hit.text for hit in result.hits] == ["Alpha semantic record."]
            assert result.hits[0].matched_by == ("fts", "vector")

            async with contexts.database.transaction() as connection:
                plan = "\n".join(
                    str(row[0]) for row in (await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)).all()
                )
            assert "TEXT RETRIEVAL SCAN" in plan
            assert "VECTOR INDEX" in plan

    asyncio.run(scenario())
