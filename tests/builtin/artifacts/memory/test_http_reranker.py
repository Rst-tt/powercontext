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

"""Behavior tests for the HTTP Memory reranking adapter."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryEntryInput, MemoryHit
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts

_ENDPOINT = "https://reranker.invalid/score"


def _hit(rank: int) -> MemoryHit:
    return MemoryHit(
        memory_ref=ArtifactRef(family="memory", artifact_id="memory", revision=1),
        entry_id=f"entry-{rank}",
        entry_version_id=f"entry-{rank}-v1",
        text=f"Candidate {rank}",
        score=1 / rank,
        matched_by=("fts",),
    )


def _scoring_handler(seen: list[dict], scores: dict[int, float]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={"scores": [{"rank": item["rank"], "score": scores[item["rank"]]} for item in body["candidates"]]},
        )

    return httpx.MockTransport(handler)


def test_http_reranker_batches_candidates_and_selects_globally() -> None:
    async def scenario() -> None:
        seen: list[dict] = []
        scores = {1: 0.10, 2: 0.90, 3: 0.95, 4: 0.90, 5: 0.20}
        async with httpx.AsyncClient(transport=_scoring_handler(seen, scores)) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=2)

            decision = await reranker.rerank("query", tuple(_hit(rank) for rank in range(1, 6)), 3)

        assert [len(body["candidates"]) for body in seen] == [2, 2, 1]
        assert [item["rank"] for body in seen for item in body["candidates"]] == [1, 2, 3, 4, 5]
        assert all(body["query"] == "query" for body in seen)
        assert decision.selected_ranks == (3, 2, 4)
        assert decision.usage.requests == 3
        assert decision.discarded_rank_count == 0
        assert decision.used_fallback is False

    asyncio.run(scenario())


def test_http_reranker_accepts_out_of_order_scores() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"scores": [{"rank": 2, "score": 0.1}, {"rank": 1, "score": 0.8}]}
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=2)

            decision = await reranker.rerank("query", (_hit(1), _hit(2)), 2)

        assert decision.selected_ranks == (1, 2)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {"scores": [{"rank": 1, "score": 0.5}], "extra": True},
        {"scores": [{"score": 0.5}]},
        {"scores": [{"rank": 1, "score": 0.5}, {"rank": 1, "score": 0.4}]},
        {"scores": [{"rank": 2, "score": 0.5}]},
        {"scores": [{"rank": 1, "score": float("nan")}]},
        {"scores": [{"rank": 1, "score": float("inf")}]},
        {"scores": [{"rank": 1, "score": 0.5, "extra": True}]},
    ],
)
def test_http_reranker_rejects_invalid_responses(payload: dict) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=json.dumps(payload).encode()))
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=1)

            with pytest.raises(ValueError):
                await reranker.rerank("query", (_hit(1),), 1)

    asyncio.run(scenario())


def test_http_reranker_rejects_invalid_json() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"not json"))
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client)

            with pytest.raises(ValueError):
                await reranker.rerank("query", (_hit(1),), 1)

    asyncio.run(scenario())


def test_http_reranker_preserves_transport_errors() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client)

            with pytest.raises(httpx.ConnectError):
                await reranker.rerank("query", (_hit(1),), 1)

        transport = httpx.MockTransport(lambda _request: httpx.Response(500, json={"scores": []}))
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client)

            with pytest.raises(httpx.HTTPStatusError):
                await reranker.rerank("query", (_hit(1),), 1)

    asyncio.run(scenario())


def test_http_reranker_validates_bounds() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200))) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client)

            with pytest.raises(ValueError):
                await reranker.rerank("query", (), 1)
            with pytest.raises(ValueError):
                await reranker.rerank("query", (_hit(1),), 2)
            with pytest.raises(ValueError):
                await reranker.rerank("query", (_hit(1),), 0)
            with pytest.raises(ValueError):
                HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=0)

    asyncio.run(scenario())


def test_http_reranker_runs_inside_native_memory_search() -> None:
    async def scenario() -> None:
        seen: list[dict] = []
        async with httpx.AsyncClient(transport=_scoring_handler(seen, {1: 0.2, 2: 0.9})) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=1)
            async with open_builtin_contexts(
                BuiltinConfig(database=SQLiteConfig()), memory_reranker=reranker
            ) as contexts:
                service = (await contexts.get("http-rerank")).artifacts.memory
                memory = await service.remember(
                    memory=None,
                    entries=(
                        MemoryEntryInput(kind="fact", text="Project alpha fact."),
                        MemoryEntryInput(kind="fact", text="Project beta fact."),
                    ),
                    mode="append",
                )
                assert memory is not None

                result = await service.search("project", memories=(memory,), limit=1, mode="fts")

        assert result.rerank is not None
        assert result.rerank.policy_id == "http-cross-encoder-v1"
        assert result.rerank.selected_ranks == (2,)
        assert result.rerank.usage.requests == len(seen)
        assert result.hits[0].entry_version_id == result.rerank.candidate_hits[1].entry_version_id

    asyncio.run(scenario())
