# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Share embedding work and successful vectors within one event loop."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from dataclasses import dataclass

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel


@dataclass(frozen=True, slots=True, eq=False)
class _BatchUsage:
    # Identity distinguishes batches even when a provider reports equal usage.
    usage: InferenceUsage


@dataclass(frozen=True, slots=True)
class _Embedded:
    vector: EmbeddingVector
    batch: _BatchUsage | None = None


class CoalescingEmbeddingModel:
    """Batch distinct pending texts without transferring ownership to a caller.

    Callers share this wrapper on one event loop. Cancelling a caller only
    cancels its wait; the wrapper retains provider tasks until they settle.
    Successful vectors use a bounded LRU, while usage belongs to the batches
    each caller waited for rather than to cached vectors.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a nonnegative integer")  # noqa: TRY003
        if (
            isinstance(coalesce_ms, bool)
            or not isinstance(coalesce_ms, (int, float))
            or not math.isfinite(coalesce_ms)
            or coalesce_ms < 0
        ):
            raise ValueError("coalesce_ms must be finite and nonnegative")  # noqa: TRY003
        self.profile = model.profile
        self._model = model
        self._max_batch_size = max_batch_size
        self._delay = coalesce_ms / 1000
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[_Embedded]] = {}
        self._pending: dict[str, asyncio.Future[_Embedded]] = {}
        self._timer: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        texts = tuple(texts)
        if not texts:
            return EmbeddingResult(vectors=())
        loop = asyncio.get_running_loop()
        selected: dict[str, _Embedded | asyncio.Future[_Embedded]] = {}
        # No await until every text is registered: concurrent callers see the
        # same pending futures, including when the merge window is zero.
        for text in texts:
            if text in self._cache:
                self._cache.move_to_end(text)
                selected[text] = _Embedded(self._cache[text])
            else:
                future = self._inflight.get(text)
                if future is None:
                    future = loop.create_future()
                    future.add_done_callback(_observe_failure)
                    self._inflight[text] = self._pending[text] = future
                selected[text] = future
        if self._pending and self._timer is None:
            self._timer = loop.call_later(self._delay, self._flush)
        futures = {item for item in selected.values() if isinstance(item, asyncio.Future)}
        if futures:
            await asyncio.gather(*(asyncio.shield(future) for future in futures))
        resolved = {
            text: item.result() if isinstance(item, asyncio.Future) else item for text, item in selected.items()
        }
        batches = {item.batch for item in resolved.values() if item.batch is not None}
        usages = [batch.usage for batch in batches]
        input_tokens = [usage.input_tokens for usage in usages if usage.input_tokens is not None]
        output_tokens = [usage.output_tokens for usage in usages if usage.output_tokens is not None]
        return EmbeddingResult(
            vectors=tuple(resolved[text].vector for text in texts),
            usage=InferenceUsage(
                requests=sum(usage.requests for usage in usages),
                input_tokens=sum(input_tokens) if input_tokens else None,
                output_tokens=sum(output_tokens) if output_tokens else None,
            ),
        )

    def _flush(self) -> None:
        self._timer = None
        pending = tuple(self._pending.items())
        self._pending.clear()
        for start in range(0, len(pending), self._max_batch_size):
            batch = pending[start : start + self._max_batch_size]
            task = asyncio.create_task(self._run_batch(batch))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_batch(self, batch: tuple[tuple[str, asyncio.Future[_Embedded]], ...]) -> None:
        try:
            result = await self._model.embed(tuple(text for text, _ in batch))
            self._publish(batch, result)
        except asyncio.CancelledError:
            for _, future in batch:
                future.cancel()
            raise
        except Exception as error:
            for _, future in batch:
                future.set_exception(error)
        finally:
            for text, future in batch:
                if self._inflight.get(text) is future:
                    del self._inflight[text]

    def _publish(self, batch: tuple[tuple[str, asyncio.Future[_Embedded]], ...], result: EmbeddingResult) -> None:
        if len(result.vectors) != len(batch):
            raise ValueError("embedding vector count does not match the requested batch")  # noqa: TRY003
        usage = _BatchUsage(result.usage.model_copy(deep=True))
        for (text, future), vector in zip(batch, result.vectors, strict=True):
            if self._cache_size:
                self._cache[text] = vector
                self._cache.move_to_end(text)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
            future.set_result(_Embedded(vector, usage))


def _observe_failure(future: asyncio.Future[_Embedded]) -> None:
    # A batch still runs when all of its callers cancel. Observe orphaned
    # failures without changing the exception delivered to remaining waiters.
    if not future.cancelled():
        future.exception()


__all__ = ["CoalescingEmbeddingModel"]
