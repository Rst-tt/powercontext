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

"""Tests for cross-call embedding request coalescing and vector caching."""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from powercontext.builtin.artifacts.memory.models import EmbeddingProfile
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel
from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage

DIMENSION = 4


def _vector(text: str) -> EmbeddingVector:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return tuple(float(digest[index]) for index in range(DIMENSION))


class _FakeEmbeddingModel:
    """Deterministic embedding model that records every provider request."""

    def __init__(self, *, fail: bool = False, vector_count_delta: int = 0) -> None:
        self.profile = EmbeddingProfile(
            profile_id="coalescing-test",
            model="fake:embed",
            dimension=DIMENSION,
            normalization="none",
        )
        self.calls: list[tuple[str, ...]] = []
        self.fail = fail
        self.vector_count_delta = vector_count_delta
        self.gate: asyncio.Event | None = None

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        self.calls.append(tuple(texts))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("provider is unavailable")
        vectors = [_vector(text) for text in texts]
        if self.vector_count_delta:
            vectors = vectors[: len(vectors) - self.vector_count_delta]
        return EmbeddingResult(
            vectors=tuple(vectors),
            usage=InferenceUsage(requests=1, input_tokens=len(texts), output_tokens=0),
        )


def test_profile_is_taken_from_the_wrapped_model() -> None:
    base = _FakeEmbeddingModel()
    wrapper = CoalescingEmbeddingModel(base)
    assert wrapper.profile is base.profile


def test_concurrent_callers_share_one_provider_request() -> None:
    async def scenario() -> None:
        base = _FakeEmbeddingModel()
        wrapper = CoalescingEmbeddingModel(base, max_batch_size=8, coalesce_ms=20)
        first, second = await asyncio.gather(
            wrapper.embed(("alpha", "beta", "alpha")),
            wrapper.embed(("beta", "gamma")),
        )
        assert first.vectors == (_vector("alpha"), _vector("beta"), _vector("alpha"))
        assert second.vectors == (_vector("beta"), _vector("gamma"))
        assert sorted(text for call in base.calls for text in call) == ["alpha", "beta", "gamma"]
        assert all(len(call) <= 8 for call in base.calls)
        assert first.usage.requests == 1
        assert second.usage.requests == 1

    asyncio.run(scenario())


def test_provider_requests_respect_the_batch_size_limit() -> None:
    async def scenario() -> None:
        base = _FakeEmbeddingModel()
        wrapper = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=0)
        result = await wrapper.embed(("a", "b", "c", "d", "e"))
        assert result.vectors == tuple(_vector(text) for text in ("a", "b", "c", "d", "e"))
        assert all(len(call) <= 2 for call in base.calls)
        assert sum(len(call) for call in base.calls) == 5

    asyncio.run(scenario())


def test_cached_text_is_not_sent_to_the_provider_again() -> None:
    async def scenario() -> None:
        base = _FakeEmbeddingModel()
        wrapper = CoalescingEmbeddingModel(base, coalesce_ms=0)
        first = await wrapper.embed(("alpha",))
        second = await wrapper.embed(("alpha",))
        assert first.vectors == second.vectors
        assert base.calls == [("alpha",)]
        assert second.usage.requests == 0

    asyncio.run(scenario())


def test_lru_eviction_recomputes_the_least_recently_used_text() -> None:
    async def scenario() -> None:
        base = _FakeEmbeddingModel()
        wrapper = CoalescingEmbeddingModel(base, cache_size=1, coalesce_ms=0)
        await wrapper.embed(("alpha",))
        await wrapper.embed(("beta",))
        await wrapper.embed(("alpha",))
        assert base.calls == [("alpha",), ("beta",), ("alpha",)]

    asyncio.run(scenario())


def test_recently_used_text_survives_eviction() -> None:
    async def scenario() -> None:
        base = _FakeEmbeddingModel()
        wrapper = CoalescingEmbeddingModel(base, cache_size=2, coalesce_ms=0)
        await wrapper.embed(("alpha",))
        await wrapper.embed(("beta",))
        await wrapper.embed(("alpha",))
        await wrapper.embed(("gamma",))
        await wrapper.embed(("alpha",))
        assert [call for call in base.calls].count(("alpha",)) == 1
        assert [call for call in base.calls].count(("beta",)) == 1
        await wrapper.embed(("beta",))
        assert [call for call in base.calls].count(("beta",)) == 2

    asyncio.run(scenario())


def test_cache_can_be_disabled_while_in_flight_work_is_still_shared() -> None:
    async def scenario() -> None:
        base = _FakeEmbeddingModel()
        wrapper = CoalescingEmbeddingModel(base, cache_size=0, coalesce_ms=20)
        first, second = await asyncio.gather(wrapper.embed(("alpha",)), wrapper.embed(("alpha",)))
        assert first.vectors == second.vectors == (_vector("alpha"),)
        assert base.calls == [("alpha",)]
        await wrapper.embed(("alpha",))
        assert base.calls == [("alpha",), ("alpha",)]

    asyncio.run(scenario())


def test_empty_input_never_reaches_the_provider() -> None:
    async def scenario() -> None:
        base = _FakeEmbeddingModel()
        wrapper = CoalescingEmbeddingModel(base)
        result = await wrapper.embed(())
        assert result.vectors == ()
        assert result.usage.requests == 0
        assert base.calls == []

    asyncio.run(scenario())


def test_failed_batch_is_reported_and_not_cached() -> None:
    async def scenario() -> None:
        base = _FakeEmbeddingModel(fail=True)
        wrapper = CoalescingEmbeddingModel(base, coalesce_ms=0)
        with pytest.raises(RuntimeError):
            await wrapper.embed(("alpha",))
        base.fail = False
        result = await wrapper.embed(("alpha",))
        assert result.vectors == (_vector("alpha"),)
        assert base.calls == [("alpha",), ("alpha",)]

    asyncio.run(scenario())


def test_one_failing_batch_does_not_block_an_independent_batch() -> None:
    class _SelectiveModel(_FakeEmbeddingModel):
        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.calls.append(tuple(texts))
            if "bad" in texts:
                raise RuntimeError("provider is unavailable")
            return EmbeddingResult(
                vectors=tuple(_vector(text) for text in texts),
                usage=InferenceUsage(requests=1, input_tokens=len(texts), output_tokens=0),
            )

    async def scenario() -> None:
        base = _SelectiveModel()
        wrapper = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0)
        failing, healthy = await asyncio.gather(
            wrapper.embed(("bad",)),
            wrapper.embed(("good",)),
            return_exceptions=True,
        )
        assert isinstance(failing, RuntimeError)
        assert healthy.vectors == (_vector("good"),)

    asyncio.run(scenario())


def test_cancelling_one_waiter_keeps_the_result_for_the_other_waiters() -> None:
    async def scenario() -> None:
        base = _FakeEmbeddingModel()
        base.gate = asyncio.Event()
        wrapper = CoalescingEmbeddingModel(base, coalesce_ms=0)
        cancelled = asyncio.create_task(wrapper.embed(("alpha",)))
        survivor = asyncio.create_task(wrapper.embed(("alpha",)))
        await asyncio.sleep(0.05)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        base.gate.set()
        assert (await survivor).vectors == (_vector("alpha"),)
        assert base.calls == [("alpha",)]

    asyncio.run(scenario())


def test_usage_counts_every_waited_batch_once() -> None:
    async def scenario() -> None:
        base = _FakeEmbeddingModel()
        wrapper = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0)
        result = await wrapper.embed(("a", "b"))
        assert result.usage.requests == 2
        assert result.usage.input_tokens == 2

    asyncio.run(scenario())


def test_vector_count_mismatch_is_reported_as_value_error() -> None:
    async def scenario() -> None:
        base = _FakeEmbeddingModel(vector_count_delta=1)
        wrapper = CoalescingEmbeddingModel(base, coalesce_ms=0)
        with pytest.raises(ValueError):
            await wrapper.embed(("alpha", "beta"))

    asyncio.run(scenario())
