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
import math
from collections import Counter

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts


def test_memory_writes_in_several_scopes_share_embedding_work_and_search_after_reopening(tmp_path):
    async def scenario():
        texts = tuple(f"Calibration evidence {index:02}" for index in range(14))
        parties = 5
        batch_size = 4

        class Embedding:
            profile = EmbeddingProfile(profile_id="coalescing-native-regression", model="offline", dimension=len(texts))

            def __init__(self):
                self.calls = []

            async def embed(self, values):
                self.calls.append(values)
                return EmbeddingResult(
                    vectors=tuple(tuple(float(text == value) for text in texts) for value in values),
                    usage=InferenceUsage(requests=2, input_tokens=len(values) * 5, output_tokens=0),
                )

        base = Embedding()
        model = CoalescingEmbeddingModel(base, max_batch_size=batch_size, coalesce_ms=0, cache_size=28)

        class StartTogether:
            profile = model.profile

            def __init__(self):
                self.arrived = 0
                self.release = asyncio.Event()

            async def embed(self, values):
                if self.arrived < parties:
                    self.arrived += 1
                    if self.arrived == parties:
                        self.release.set()
                    await self.release.wait()
                return await model.embed(values)

        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'coalescing.db'}"))
        async with open_builtin_contexts(config, embedding_model=StartTogether()) as contexts:
            services = [(await contexts.get(f"calibration-{index}")).artifacts.memory for index in range(parties)]
            entries = tuple(MemoryEntryInput(kind="fact", text=text) for text in texts)
            memories = await asyncio.wait_for(
                asyncio.gather(
                    *(service.remember(memory=None, entries=entries, mode="append") for service in services)
                ),
                timeout=30,
            )
            assert all(memory is not None for memory in memories)
            assert len(base.calls) == math.ceil(len(texts) / batch_size)
            assert all(1 <= len(batch) <= batch_size for batch in base.calls)
            assert Counter(text for batch in base.calls for text in batch) == Counter(texts)
            for index, (service, memory) in enumerate(zip(services, memories, strict=True)):
                assert memory is not None
                result = await service.search(texts[index], memories=(memory,), mode="vector", limit=1)
                assert result.hits[0].text == texts[index]
            assert len(base.calls) == 4

        # Reopen the actual SQLite database and use the public native search
        # path again. The wrapper remains shared and its successful cache warm.
        async with open_builtin_contexts(config, embedding_model=model) as contexts:
            for index, memory in enumerate(memories):
                assert memory is not None
                service = (await contexts.get(f"calibration-{index}")).artifacts.memory
                stored = await service.get(memory)
                result = await service.search(texts[index + parties], memories=(stored,), mode="vector", limit=1)
                assert result.hits[0].text == texts[index + parties]
            assert len(base.calls) == 4

    asyncio.run(scenario())
