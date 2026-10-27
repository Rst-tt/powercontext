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

"""Cross-call embedding request coalescing and bounded vector caching.

``CoalescingEmbeddingModel`` wraps one embedding model so that text submitted
by concurrent callers is sent to the provider once, and so that vectors that
were computed successfully are reused by later callers. Every caller still
receives the vectors in the order of its own input, the failure of one provider
batch stays local to the callers waiting for that batch, cancelling one caller
never cancels shared provider work, and each caller reports the usage of the
distinct provider batches it waited for.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from powercontext.builtin.inference.errors import InvalidInferenceOutputError
from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import QueryEmbeddingModel

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import EmbeddingProfile
    from powercontext.builtin.inference.protocols import EmbeddingModel

EmbedCall = Callable[[tuple[str, ...]], Awaitable[EmbeddingResult]]


@dataclass(slots=True, eq=False)
class _Batch:
    """One provider request shared by every caller waiting for its texts."""

    namespace: _Namespace
    texts: list[str]
    future: asyncio.Future[EmbeddingResult]
    vectors: dict[str, EmbeddingVector] = field(default_factory=dict)
    usage: InferenceUsage | None = None
    task: asyncio.Task[None] | None = None


@dataclass(slots=True, eq=False)
class _Namespace:
    """Coalescing state of one embedding namespace."""

    call: EmbedCall
    cache: OrderedDict[str, EmbeddingVector] = field(default_factory=OrderedDict)
    batches: dict[str, _Batch] = field(default_factory=dict)
    # The batch still accepting texts inside its coalescing window; a dispatched
    # batch leaves this slot because provider requests cannot grow afterwards.
    filling: _Batch | None = None


def _consume_failure(future: asyncio.Future[EmbeddingResult]) -> None:
    """Mark a batch failure as observed so abandoned batches stay silent."""

    if not future.cancelled():
        future.exception()


class CoalescingEmbeddingModel:
    """Merge concurrent embedding calls and cache the vectors they produce.

    Text claimed by different callers inside the same window is sent to the
    provider once, and every caller receives the vectors in the order of its
    own input. Successful vectors are kept in a bounded LRU cache, so later
    calls reuse them without touching the provider.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be at least 1")  # noqa: TRY003
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must not be negative")  # noqa: TRY003
        if cache_size < 0:
            raise ValueError("cache_size must not be negative")  # noqa: TRY003
        self._model = model
        self.profile: EmbeddingProfile = model.profile
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_ms / 1000
        self._cache_size = cache_size
        self._document_namespace = _Namespace(call=model.embed)
        if isinstance(model, QueryEmbeddingModel):
            self._query_namespace = _Namespace(call=model.embed_query)
        else:
            self._query_namespace = self._document_namespace

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Embed one ordered batch, sharing pending work with concurrent callers."""

        return await self._embed(texts, self._document_namespace)

    async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Embed retrieval queries under the query namespace of the wrapped model."""

        return await self._embed(texts, self._query_namespace)

    async def _embed(self, texts: tuple[str, ...], namespace: _Namespace) -> EmbeddingResult:
        requested = tuple(texts)
        if not requested:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0, input_tokens=0, output_tokens=0))

        cached: dict[str, EmbeddingVector] = {}
        owned: dict[str, _Batch] = {}
        fresh: list[str] = []
        for text in dict.fromkeys(requested):
            vector = self._cached(namespace, text)
            if vector is not None:
                cached[text] = vector
                continue
            batch = namespace.batches.get(text)
            if batch is not None:
                owned[text] = batch
                continue
            fresh.append(text)
        owned.update(self._claim(namespace, fresh))

        waited: dict[_Batch, EmbeddingResult] = {}
        for batch in dict.fromkeys(owned.values()):
            waited[batch] = await asyncio.shield(batch.future)

        vectors: list[EmbeddingVector] = []
        for text in requested:
            vector = cached.get(text)
            if vector is None:
                vector = owned[text].vectors[text]
            vectors.append(vector)
        return EmbeddingResult(vectors=tuple(vectors), usage=self._usage(waited.values()))

    def _claim(self, namespace: _Namespace, texts: list[str]) -> dict[str, _Batch]:
        claimed: dict[str, _Batch] = {}
        for text in texts:
            existing = namespace.batches.get(text)
            if existing is not None:
                claimed[text] = existing
                continue
            batch = namespace.filling
            if batch is None or len(batch.texts) >= self._max_batch_size:
                batch = _Batch(namespace=namespace, texts=[], future=self._future())
                namespace.filling = batch
                batch.task = asyncio.create_task(self._dispatch(batch))
            batch.texts.append(text)
            namespace.batches[text] = batch
            claimed[text] = batch
        return claimed

    async def _dispatch(self, batch: _Batch) -> None:
        try:
            await asyncio.sleep(self._coalesce_seconds)
        except asyncio.CancelledError:
            self._abandon(batch)
            raise
        namespace = batch.namespace
        if namespace.filling is batch:
            namespace.filling = None
        await self._run(batch, tuple(batch.texts))

    async def _run(self, batch: _Batch, texts: tuple[str, ...]) -> None:
        namespace = batch.namespace
        try:
            result = await namespace.call(texts)
        except asyncio.CancelledError:
            self._abandon(batch)
            raise
        except Exception as error:
            self._fail(batch, error)
            return
        vectors = tuple(result.vectors)
        if len(vectors) != len(texts):
            self._fail(
                batch,
                InvalidInferenceOutputError("embed", f"expected {len(texts)} vectors but received {len(vectors)}"),
            )
            return
        batch.vectors = dict(zip(texts, vectors, strict=True))
        batch.usage = result.usage
        for text, vector in zip(texts, vectors, strict=True):
            self._store(namespace, text, vector)
            namespace.batches.pop(text, None)
        batch.future.set_result(result)

    def _fail(self, batch: _Batch, error: BaseException) -> None:
        namespace = batch.namespace
        for text in batch.texts:
            namespace.batches.pop(text, None)
        if not batch.future.done():
            batch.future.set_exception(error)

    def _abandon(self, batch: _Batch) -> None:
        namespace = batch.namespace
        if namespace.filling is batch:
            namespace.filling = None
        for text in batch.texts:
            namespace.batches.pop(text, None)
        if not batch.future.done():
            batch.future.cancel()

    def _future(self) -> asyncio.Future[EmbeddingResult]:
        future: asyncio.Future[EmbeddingResult] = asyncio.get_running_loop().create_future()
        future.add_done_callback(_consume_failure)
        return future

    def _cached(self, namespace: _Namespace, text: str) -> EmbeddingVector | None:
        if self._cache_size <= 0:
            return None
        vector = namespace.cache.get(text)
        if vector is None:
            return None
        namespace.cache.move_to_end(text)
        return vector

    def _store(self, namespace: _Namespace, text: str, vector: EmbeddingVector) -> None:
        if self._cache_size <= 0:
            return
        namespace.cache[text] = vector
        namespace.cache.move_to_end(text)
        while len(namespace.cache) > self._cache_size:
            namespace.cache.popitem(last=False)

    def _usage(self, batches: Iterable[_Batch]) -> InferenceUsage:
        requests = 0
        input_tokens = 0
        output_tokens = 0
        input_known = True
        output_known = True
        for batch in batches:
            usage = batch.usage
            if usage is None:
                continue
            requests += usage.requests
            if usage.input_tokens is None:
                input_known = False
            else:
                input_tokens += usage.input_tokens
            if usage.output_tokens is None:
                output_known = False
            else:
                output_tokens += usage.output_tokens
        return InferenceUsage(
            requests=requests,
            input_tokens=input_tokens if input_known else None,
            output_tokens=output_tokens if output_known else None,
        )


__all__ = ["CoalescingEmbeddingModel"]
