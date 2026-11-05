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
import json
from pathlib import Path

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    RememberMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.context_pack import ContextPack, ContextPackDelta, apply_context_pack_delta
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft


def test_selected_evidence_survives_restart_incremental_transport_and_source_lifecycle(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'evidence.db'}"))
        text = (
            "Office inventory notes. " * 80
            + "Copper calibration requires a 17 degree bath. "
            + "Routine instrument notes. " * 80
            + "Tungsten valve pressure must stay below 24 units."
        )
        requests = tuple(
            PrepareContextRequest(query=query, evidence_selection=True, max_bytes=1400)
            for query in ("copper calibration", "tungsten valve pressure")
        )
        saved_items = []
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Evidence", summary="Literal passages", idempotency_key="evidence-selection")
            )
            sid = scope.scope_id
            memory = runtime.memory.for_scope(sid)
            captured = await runtime.sources.for_scope(sid).capture(
                CaptureSource(source_id="manual", content="Laboratory manual.", metadata={})
            )
            result = await memory.remember(
                RememberMemoryRequest(entries=(MemoryEntryInput(kind="document", text=text),))
            )
            assert result.entry is not None
            lifecycle = runtime.lifecycle.for_scope(sid)
            await lifecycle.register_source("manual", captured.source_ref, revision=1)
            await lifecycle.register_memory("instrument", result.entry.citation, parents=("manual",), policy="all")
            context = runtime.context.for_scope(sid)
            base = None
            prepared_contents = []
            for request in requests:
                prepared = await context.prepare(request)
                assert prepared.content and prepared.content_bytes <= request.max_bytes
                prepared_contents.append(prepared.content)
                items = json.loads(prepared.content.splitlines()[-2])["items"]
                assert items and any(request.query.split()[0] in item["content"].casefold() for item in items)
                for item in items:
                    assert item["citation"]["scope_id"] == sid
                    citation = MemoryCitation.model_validate({
                        k: v for k, v in item["citation"].items() if k != "scope_id"
                    })
                    entry = await memory.get(GetMemoryEntryRequest(citation=citation))
                    assert entry.entry.text[item["start"] : item["end"]] == item["content"]
                    saved_items.append((citation, item))
                wire = await context.prepare_incremental(request, base_pack=base)
                if base is None:
                    assert isinstance(wire, ContextPack)
                    base = ContextPack.from_bytes(wire.to_bytes())
                else:
                    assert isinstance(wire, ContextPackDelta)
                    base = apply_context_pack_delta(base, ContextPackDelta.from_bytes(wire.to_bytes()))
                assert b"".join(base.items).decode() == prepared.content
            assert prepared_contents[0] != prepared_contents[1]

        async with open_builtin_runtime(config) as runtime:
            context = runtime.context.for_scope(sid)
            assert (await context.prepare(requests[-1])).content == prepared_contents[-1]
            lifecycle = runtime.lifecycle.for_scope(sid)
            await lifecycle.apply(
                SourceLifecycleEvent(
                    node_id="manual", revision=1, sequence=1, available=False, reason="Manual withdrawn"
                )
            )
            assert (await context.prepare(requests[-1])).content is None
            for citation, item in saved_items:
                record = await runtime.memory.for_scope(sid).get(GetMemoryEntryRequest(citation=citation))
                assert record.entry.text[item["start"] : item["end"]] == item["content"]
            await lifecycle.apply(
                SourceLifecycleEvent(node_id="manual", revision=1, sequence=2, available=True, reason="Manual restored")
            )
            assert (await context.prepare(requests[-1])).content

    asyncio.run(scenario())
