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
import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.service import MemoryService
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    ContextAssembly,
    ContextAssemblySection,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft


async def _seed(runtime, key):
    scope = await runtime.scopes.create(ScopeDraft(title=key, summary="Lifecycle regression", idempotency_key=key))
    sid = scope.scope_id
    lifecycle = runtime.lifecycle.for_scope(sid)
    references = {}
    for root in ("left", "right"):
        receipt = await runtime.sources.for_scope(sid).capture(CaptureSource(source_id=root, content=root, metadata={}))
        references[root] = receipt.source_ref
        await lifecycle.register_source(root, receipt.source_ref, revision=3)
    for node, parents, policy in (
        ("alpha", ("left",), "all"),
        ("beta", ("right",), "all"),
        ("joined", ("alpha", "beta"), "all"),
        ("fallback", ("alpha", "beta"), "any"),
    ):
        result = await runtime.memory.for_scope(sid).remember(
            RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=f"{node} lifecycle evidence"),))
        )
        assert result.entry is not None
        references[node] = result.entry.citation
        await lifecycle.register_memory(node, result.entry.citation, parents=parents, policy=policy)
    for node, parents in (("sharedtopic", ("joined",)), ("independenttopic", ("beta",))):
        artifact = await runtime.records.for_scope(sid).create_artifact(
            "topic-memory",
            ArtifactWrite(content={"title": node, "summary": f"{node} lifecycle summary", "detail": node}),
        )
        ref = ArtifactRef(family="topic-memory", artifact_id=artifact.artifact_id, revision=artifact.revision)
        references[node] = ref
        await lifecycle.register_topic(node, ref, parents=parents, policy="all")
    return sid, lifecycle, references


def _event(sequence, available):
    return SourceLifecycleEvent(
        node_id="left", revision=3, sequence=sequence, available=available, reason="source change"
    )


async def _assert_withdrawn(runtime, sid, references):
    records = await runtime.memory.for_scope(sid).list(include_inactive=True)
    assert {record.entry.text.split()[0] for record in records.entries if record.state == "active"} == {
        "beta",
        "fallback",
    }
    for node in ("alpha", "joined"):
        result = await runtime.memory.for_scope(sid).search(SearchMemoryRequest(query=node, mode="fts"))
        assert not result.hits
        original = await runtime.memory.for_scope(sid).get(GetMemoryEntryRequest(citation=references[node]))
        assert original.entry.text == f"{node} lifecycle evidence"
    topics = await runtime.topic_memory.for_scope(sid).search(SearchTopicMemoryRequest(query="lifecycle", limit=1))
    assert len(topics.hits) == 1 and topics.hits[0].title == "independenttopic"
    browsed = await runtime.topic_memory.for_scope(sid).browse(limit=1)
    assert len(browsed) == 1 and browsed[0].title == "independenttopic"
    prepared = await runtime.context.for_scope(sid).prepare(
        PrepareContextRequest(
            query="lifecycle",
            max_bytes=4096,
            assembly=ContextAssembly(sections=(ContextAssemblySection(family="topic-memory", limit=1),)),
        )
    )
    assert "independenttopic lifecycle summary" in (prepared.content or "")
    assert "sharedtopic lifecycle summary" not in (prepared.content or "")


def test_scope_isolation_original_references_and_replay_survive_reopening(tmp_path):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'isolation.db'}"))
        async with open_builtin_runtime(config) as runtime:
            sid, lifecycle, references = await _seed(runtime, "first")
            other, _, _ = await _seed(runtime, "second")
            assert await lifecycle.apply(_event(1, False)) == ["beta", "fallback", "independenttopic", "right"]
            await _assert_withdrawn(runtime, sid, references)
            assert len((await runtime.memory.for_scope(other).list()).entries) == 4
            head = (await runtime.memory.for_scope(sid).list()).memory_ref
        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(sid)
            await _assert_withdrawn(runtime, sid, references)
            await lifecycle.apply(_event(1, False))
            assert (await runtime.memory.for_scope(sid).list()).memory_ref == head
            for node in ("alpha", "joined", "sharedtopic"):
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.resolve(node, purpose="generation")
                assert await lifecycle.resolve(node, purpose="audit") == references[node]
            await lifecycle.apply(_event(2, True))
            for node, ref in references.items():
                assert await lifecycle.resolve(node, purpose="generation") == ref
            assert len((await runtime.memory.for_scope(sid).list()).entries) == 4
            assert len((await runtime.memory.for_scope(other).list()).entries) == 4

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["database-abort", "lost-acknowledgement"])
def test_interrupted_native_state_transition_retries_after_reopening_without_extra_revision(
    tmp_path, monkeypatch, failure
):
    async def scenario():
        database = tmp_path / "failure.db"
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))
        original_forget = MemoryService.forget

        async def forget_then_fail(self, *args, **kwargs):
            await original_forget(self, *args, **kwargs)
            raise RuntimeError

        async with open_builtin_runtime(config) as runtime:
            sid, lifecycle, references = await _seed(runtime, "failure")
            before = (await runtime.memory.for_scope(sid).list()).memory_ref
            if failure == "database-abort":
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        "CREATE TRIGGER block_lifecycle BEFORE INSERT ON pc_artifacts "
                        "WHEN NEW.family = 'memory' BEGIN SELECT RAISE(ABORT, 'lifecycle storage failure'); END"
                    )
            else:
                monkeypatch.setattr(MemoryService, "forget", forget_then_fail)
            with pytest.raises(IntegrityError if failure == "database-abort" else RuntimeError):
                await lifecycle.apply(_event(1, False))
            after_failure = (await runtime.memory.for_scope(sid).list()).memory_ref
            if failure == "database-abort":
                assert after_failure == before
                with sqlite3.connect(database) as connection:
                    connection.execute("DROP TRIGGER block_lifecycle")
            else:
                assert after_failure != before
                monkeypatch.setattr(MemoryService, "forget", original_forget)
        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(sid)
            await lifecycle.apply(_event(1, False))
            await _assert_withdrawn(runtime, sid, references)
            head = (await runtime.memory.for_scope(sid).list()).memory_ref
            if failure == "lost-acknowledgement":
                assert head == after_failure
            await lifecycle.apply(_event(1, False))
            assert (await runtime.memory.for_scope(sid).list()).memory_ref == head
            await lifecycle.apply(_event(2, True))
            assert len((await runtime.memory.for_scope(sid).list()).entries) == 4

    asyncio.run(scenario())


def test_registration_validates_readable_references_dependencies_and_idempotency():
    async def scenario():
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            sid, lifecycle, refs = await _seed(runtime, "validation")
            before = (await runtime.memory.for_scope(sid).list()).memory_ref
            await lifecycle.register_memory("joined", refs["joined"], parents=("beta", "alpha"), policy="all")
            assert (await runtime.memory.for_scope(sid).list()).memory_ref == before
            for parents in ((), ("unknown",), ("left", "left"), ("new",)):
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.register_memory("new", refs["alpha"], parents=parents, policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source(
                    "missing", refs["left"].model_copy(update={"source_id": "absent"}), revision=3
                )
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_topic(
                    "missing",
                    ArtifactRef(family="topic-memory", artifact_id="absent", revision=1),
                    parents=("left",),
                    policy="all",
                )
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(
                    SourceLifecycleEvent(
                        node_id="alpha", revision=3, sequence=1, available=False, reason="not a source"
                    )
                )
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("missing", purpose="audit")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("alpha", purpose="other")
            assert (await runtime.memory.for_scope(sid).list()).memory_ref == before

    asyncio.run(scenario())


def test_full_event_reason_survives_native_audit_limit_and_keeps_replay_identity(tmp_path):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'reason.db'}"))
        event = SourceLifecycleEvent(node_id="left", revision=3, sequence=1, available=False, reason="原始原因" * 200)
        async with open_builtin_runtime(config) as runtime:
            sid, lifecycle, refs = await _seed(runtime, "reason")
            await lifecycle.apply(event)
            await _assert_withdrawn(runtime, sid, refs)
            head = (await runtime.memory.for_scope(sid).list()).memory_ref
        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(sid)
            await lifecycle.apply(event)
            assert (await runtime.memory.for_scope(sid).list()).memory_ref == head
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(event.model_copy(update={"reason": event.reason + "x"}))

    asyncio.run(scenario())
