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

from __future__ import annotations

import asyncio
import gc
from collections import Counter

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel


def _vector(text: str) -> tuple[float, ...]:
    return (float(len(text)), float(sum(map(ord, text))))


class _Embedding:
    profile = EmbeddingProfile(profile_id="coalescing-test", model="offline", dimension=2)

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
        self.calls.append(texts)
        return EmbeddingResult(
            vectors=tuple(_vector(text) for text in texts),
            usage=InferenceUsage(requests=3, input_tokens=len(texts) * 7, output_tokens=2),
        )


@pytest.mark.parametrize("cache_size", [0, 8])
def test_late_waiter_shares_running_texts_and_receives_each_batch_usage_once(cache_size):
    class Gated(_Embedding):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts):
            result = await super().embed(texts)
            self.started.set()
            await self.release.wait()
            return result

    async def scenario():
        base = Gated()
        model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=0, cache_size=cache_size)
        first = asyncio.create_task(model.embed(("alpha", "beta", "alpha")))
        await base.started.wait()
        late = asyncio.create_task(model.embed(("beta", "gamma", "alpha", "gamma")))
        # The new text may be batched independently while the first request is
        # still running. Repeated/in-flight texts must not be submitted again.
        await asyncio.sleep(0)
        base.release.set()
        a, b = await asyncio.gather(first, late)
        assert model.profile is base.profile
        assert a.vectors == tuple(map(_vector, ("alpha", "beta", "alpha")))
        assert b.vectors == tuple(map(_vector, ("beta", "gamma", "alpha", "gamma")))
        assert Counter(text for batch in base.calls for text in batch) == Counter(("alpha", "beta", "gamma"))
        assert a.usage == InferenceUsage(requests=3, input_tokens=14, output_tokens=2)
        assert b.usage == InferenceUsage(requests=6, input_tokens=21, output_tokens=4)
        again = await model.embed(("alpha",))
        assert again.usage.requests == (3 if cache_size == 0 else 0)

    asyncio.run(scenario())


def test_per_caller_usage_includes_full_shared_batches_with_cache_hits_uncharged():
    async def scenario():
        base = _Embedding()
        model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=10)
        await model.embed(("cached",))
        before = len(base.calls)
        inputs = (("cached", "a", "b", "a", "d"), ("b", "c", "c"))
        results = await asyncio.gather(*(model.embed(texts) for texts in inputs))
        batches = base.calls[before:]
        assert len(batches) == 2
        assert all(1 <= len(batch) <= 2 for batch in batches)
        assert Counter(text for batch in batches for text in batch) == Counter(("a", "b", "c", "d"))
        for texts, result in zip(inputs, results, strict=True):
            waited = [batch for batch in batches if set(batch) & set(texts)]
            assert result.vectors == tuple(map(_vector, texts))
            assert result.usage == InferenceUsage(
                requests=3 * len(waited), input_tokens=7 * sum(map(len, waited)), output_tokens=2 * len(waited)
            )
        cached = await model.embed(("cached", "a", "b", "c", "d"))
        assert cached.usage.requests == 0 and len(base.calls) == before + 2

    asyncio.run(scenario())


def test_one_failed_batch_does_not_poison_shared_retries_or_independent_batches():
    class Flaky(_Embedding):
        def __init__(self):
            super().__init__()
            self.failed = False

        async def embed(self, texts):
            result = await super().embed(texts)
            if "bad" in texts and not self.failed:
                self.failed = True
                raise LookupError
            return result

    async def scenario():
        base = Flaky()
        model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=10)
        results = await asyncio.gather(
            model.embed(("bad", "bad")), model.embed(("bad",)), model.embed(("good",)), return_exceptions=True
        )
        assert isinstance(results[0], LookupError) and isinstance(results[1], LookupError)
        assert isinstance(results[2], EmbeddingResult) and results[2].vectors == (_vector("good"),)
        retry = await model.embed(("bad", "good", "bad"))
        assert retry.vectors == tuple(map(_vector, ("bad", "good", "bad")))
        assert retry.usage.requests == 3
        assert Counter(text for batch in base.calls for text in batch) == Counter({"bad": 2, "good": 1})

    asyncio.run(scenario())


@pytest.mark.parametrize("difference", [-1, 1])
def test_vector_count_failure_caches_no_partial_results(difference):
    class WrongCount(_Embedding):
        async def embed(self, texts):
            result = await super().embed(texts)
            if len(self.calls) == 1:
                return EmbeddingResult(vectors=tuple((1.0, 1.0) for _ in range(len(texts) + difference)))
            return result

    async def scenario():
        base = WrongCount()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        with pytest.raises(ValueError):
            await model.embed(("first", "second"))
        retry = await model.embed(("second", "first"))
        assert retry.vectors == (_vector("second"), _vector("first"))
        assert base.calls == [("first", "second"), ("second", "first")]

    asyncio.run(scenario())


def test_cancellation_before_dispatch_does_not_remove_surviving_waiter():
    async def scenario():
        base = _Embedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=20, cache_size=0)
        cancelled = asyncio.create_task(model.embed(("shared",)))
        survivor = asyncio.create_task(model.embed(("shared", "second", "shared")))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        result = await asyncio.wait_for(survivor, timeout=2)
        assert result.vectors == tuple(map(_vector, ("shared", "second", "shared")))
        assert result.usage.requests == 3
        assert len(base.calls) == 1

    asyncio.run(scenario())


def test_cancelled_callers_leave_no_unobserved_failure_and_retry_can_finish():
    class OrphanedFailure(_Embedding):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.finished = asyncio.Event()

        async def embed(self, texts):
            result = await super().embed(texts)
            if len(self.calls) == 1:
                self.started.set()
                await self.release.wait()
                self.finished.set()
                raise LookupError
            return result

    async def scenario():
        base = OrphanedFailure()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=0)
        loop = asyncio.get_running_loop()
        errors = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        try:
            callers = [asyncio.create_task(model.embed(("orphan",))) for _ in range(2)]
            await base.started.wait()
            for caller in callers:
                caller.cancel()
            await asyncio.gather(*callers, return_exceptions=True)
            base.release.set()
            await base.finished.wait()
            await asyncio.sleep(0)
            assert (await model.embed(("orphan",))).vectors == (_vector("orphan"),)
            gc.collect()
            await asyncio.sleep(0)
            assert errors == []
        finally:
            loop.set_exception_handler(previous)

    asyncio.run(scenario())


def test_lru_reads_follow_repeated_input_order_and_preserve_exact_strings():
    async def scenario():
        base = _Embedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=2)
        await model.embed(("a", "b"))
        await model.embed(("a", "b", "a"))
        await model.embed(("c",))
        assert (await model.embed(("a",))).usage.requests == 0
        assert (await model.embed(("b",))).usage.requests == 3
        exact = ("", " a", "a ", "a", "é", "e\u0301", "中文\n🙂")
        result = await model.embed(exact)
        assert result.vectors == tuple(map(_vector, exact))
        assert set(exact).issubset({text for batch in base.calls for text in batch})

    asyncio.run(scenario())
