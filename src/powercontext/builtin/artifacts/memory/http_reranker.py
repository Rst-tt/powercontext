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

"""Score coarse Memory candidates with an external HTTP reranking service.

The adapter speaks one offline scoring protocol: every request carries the query and a
bounded slice of the coarse pool, and every response carries the scores of exactly the
ranks that were requested. Candidates keep their original one-based rank across batch
boundaries, so the final selection is always decided by the scores of the complete pool.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision, _validate_bounds
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"


class _StrictModel(BaseModel):
    """A protocol model that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class HTTPMemoryRerankCandidate(_StrictModel):
    """One original candidate identity and its text as sent to the scoring service."""

    rank: int = Field(ge=1)
    text: str


class HTTPMemoryRerankRequest(_StrictModel):
    """The scoring protocol request body."""

    query: str
    candidates: tuple[HTTPMemoryRerankCandidate, ...]


class HTTPMemoryRerankScore(_StrictModel):
    """One scored candidate as returned by the scoring service."""

    rank: int = Field(ge=1)
    score: float = Field(allow_inf_nan=False)


class HTTPMemoryRerankResponse(_StrictModel):
    """The scoring protocol response body."""

    scores: tuple[HTTPMemoryRerankScore, ...]


class HTTPMemoryReranker:
    """Select Memory hits with the scores of an external HTTP reranking service."""

    policy_id: str

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = HTTP_RERANK_POLICY_ID,
    ) -> None:
        if not endpoint:
            raise ValueError("http reranker requires a scoring endpoint")  # noqa: TRY003
        if batch_size < 1:
            raise ValueError("http reranker batch size must be positive")  # noqa: TRY003
        if not policy_id:
            raise ValueError("http reranker policy id must not be empty")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
    ) -> MemoryRerankDecision:
        """Score the coarse pool in batches and keep the original ranks of the best hits."""

        _validate_bounds(len(candidates), limit)
        scored: list[tuple[int, float]] = []
        requests = 0
        for batch in _batches(candidates, self._batch_size):
            payload = HTTPMemoryRerankRequest(
                query=query,
                candidates=tuple(
                    HTTPMemoryRerankCandidate(rank=rank, text=candidate.text) for rank, candidate in batch
                ),
            )
            response = await self._client.post(self._endpoint, json=payload.model_dump(mode="json"))
            requests += 1
            scored.extend(_validated_scores(response, expected={rank for rank, _ in batch}))
        selected = tuple(rank for rank, _ in sorted(scored, key=lambda item: (-item[1], item[0]))[:limit])
        return MemoryRerankDecision(
            selected_ranks=selected,
            usage=InferenceUsage(requests=requests),
            discarded_rank_count=0,
            used_fallback=False,
        )


def _batches(candidates: tuple[MemoryHit, ...], batch_size: int) -> list[tuple[tuple[int, MemoryHit], ...]]:
    """Split the coarse pool into batches that keep the original one-based ranks."""

    items = tuple(enumerate(candidates, start=1))
    return [items[index : index + batch_size] for index in range(0, len(items), batch_size)]


def _validated_scores(response: httpx.Response, *, expected: set[int]) -> list[tuple[int, float]]:
    """Validate one batch response and return the scores of the requested ranks."""

    if not response.is_success:
        response.raise_for_status()
    try:
        payload = HTTPMemoryRerankResponse.model_validate(response.json())
    except ValidationError as error:
        raise ValueError(  # noqa: TRY003
            f"http reranker response does not match the scoring protocol: {error}"
        ) from error
    except ValueError as error:
        raise ValueError("http reranker response is not valid JSON") from error  # noqa: TRY003
    ranks = [item.rank for item in payload.scores]
    if len(ranks) != len(expected) or set(ranks) != expected:
        raise ValueError("http reranker response does not cover the requested ranks exactly once")  # noqa: TRY003
    return [(item.rank, item.score) for item in payload.scores]


__all__ = [
    "HTTP_RERANK_POLICY_ID",
    "HTTPMemoryRerankCandidate",
    "HTTPMemoryRerankRequest",
    "HTTPMemoryRerankResponse",
    "HTTPMemoryRerankScore",
    "HTTPMemoryReranker",
]
