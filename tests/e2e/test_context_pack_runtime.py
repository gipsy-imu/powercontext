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

from __future__ import annotations

import asyncio
from pathlib import Path

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    ContextAssembly,
    PrepareContextRequest,
    RememberMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.context_pack import ContextPack, ContextPackDelta, apply_context_pack_delta
from powercontext.builtin.scope import ScopeDraft


def test_incremental_prepare_preserves_cited_text_across_append_query_switch_and_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'incremental.db'}"))
        authorized = []

        async def authorize(scopes: tuple[str, ...]) -> None:
            authorized.append(scopes)

        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Context pack", summary="Round trips", idempotency_key="pack")
            )
            context = runtime.context.for_scope(scope.scope_id)
            memory = runtime.memory.for_scope(scope.scope_id)
            request = PrepareContextRequest(query="calibration", max_bytes=4096)
            first = await context.prepare_incremental(request, authorize_scopes=authorize)
            assert isinstance(first, ContextPack) and first.items == ()
            assert authorized == [(scope.scope_id,)]
            base = ContextPack.from_bytes(first.to_bytes())

            await memory.remember(
                RememberMemoryRequest(
                    entries=(
                        MemoryEntryInput(kind="fact", text="Calibration 原始记录 with exact source evidence."),
                        MemoryEntryInput(kind="fact", text="Design choices retain their original text."),
                    )
                )
            )
            expected = await context.prepare(request)
            assert expected.content
            update = await context.prepare_incremental(request, base_pack=base)
            assert isinstance(update, ContextPackDelta)
            base = apply_context_pack_delta(base, ContextPackDelta.from_bytes(update.to_bytes()))
            assert b"".join(base.items).decode() == expected.content

            await memory.remember(
                RememberMemoryRequest(
                    entries=(MemoryEntryInput(kind="fact", text="Calibration appended evidence 北京."),)
                )
            )
            queries = [request, PrepareContextRequest(query="design", max_bytes=4096)]
            for next_request in queries:
                expected = await context.prepare(next_request)
                assert expected.content
                update = await context.prepare_incremental(
                    next_request, base_pack=ContextPack.from_bytes(base.to_bytes())
                )
                assert isinstance(update, ContextPackDelta)
                base = apply_context_pack_delta(base, ContextPackDelta.from_bytes(update.to_bytes()))
                assert b"".join(base.items).decode() == expected.content
            saved = base.to_bytes()

        async with open_builtin_runtime(config) as runtime:
            context = runtime.context.for_scope(scope.scope_id)
            expected = await context.prepare(queries[-1])
            restored = ContextPack.from_bytes(saved)
            update = await context.prepare_incremental(queries[-1], base_pack=restored)
            assert isinstance(update, ContextPackDelta)
            target = apply_context_pack_delta(restored, ContextPackDelta.from_bytes(update.to_bytes()))
            assert b"".join(target.items).decode() == expected.content

            empty_request = PrepareContextRequest(query="calibration", assembly=ContextAssembly(sections=()))
            ordinary_empty = await context.prepare(empty_request)
            assert ordinary_empty.content is None
            empty_delta = await context.prepare_incremental(empty_request, base_pack=target)
            assert isinstance(empty_delta, ContextPackDelta)
            assert apply_context_pack_delta(target, ContextPackDelta.from_bytes(empty_delta.to_bytes())).items == ()

    asyncio.run(scenario())
