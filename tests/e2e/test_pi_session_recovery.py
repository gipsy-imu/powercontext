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

import pytest
from pydantic import JsonValue

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryCitation, MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    BuiltinRuntime,
    CommitConnectorCheckpoint,
    GetMemoryEntryRequest,
    SearchMemoryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.errors import ConnectorSubmissionRejectedError
from powercontext.sources import (
    ConnectorBinding,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
    SourceDefinitionRegistry,
    SourceObservation,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.connectors import ConnectorLifecycle
from powercontext.sources.pi_session import PI_EVENT_SOURCE_DEFINITION, PiEventInput, PiSessionConnector
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def _write(path: Path, records: list[dict[str, JsonValue]], *, sort_keys: bool = False) -> None:
    header = {"type": "session", "version": 3, "id": "recovery"}
    path.write_text("\n".join(json.dumps(row, sort_keys=sort_keys) for row in [header, *records]) + "\n")


def _message(event_id: str, parent: str | None, text: str) -> dict[str, JsonValue]:
    return {"type": "message", "id": event_id, "parentId": parent, "message": {"role": "user", "content": text}}


class _RuntimeSink:
    def __init__(self, runtime: BuiltinRuntime, *, fail_event: str | None, reject_event: str | None) -> None:
        self.runtime = runtime
        self.registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        self.fail_event, self.reject_event = fail_event, reject_event

    async def submit(self, binding: ConnectorBinding, item_id: str, definition_name: str, value: object, /):
        assert isinstance(value, PiEventInput)
        if value.event_id == self.fail_event:
            raise OSError("injected submission failure")  # noqa: TRY003
        if value.event_id == self.reject_event:
            raise ConnectorSubmissionRejectedError("injected submission rejection")  # noqa: TRY003
        source = await self.registry.resolve(value)
        receipt = await self.runtime.ingestion.submit(
            SubmitSourceObservation(
                scope_id=binding.scope_id, observation=project_source_for_transport(self.registry, source)
            )
        )
        return receipt.source_ref


class _RuntimeCheckpoints:
    def __init__(self, runtime: BuiltinRuntime) -> None:
        self.runtime = runtime

    async def load(self, binding: ConnectorBinding, /) -> JsonValue | None:
        return (await self.runtime.ingestion.checkpoint(binding)).checkpoint

    async def save(
        self, binding: ConnectorBinding, checkpoint: JsonValue | None, /, *, expected: JsonValue | None
    ) -> None:
        await self.runtime.ingestion.commit(
            CommitConnectorCheckpoint(binding=binding, checkpoint=checkpoint, expected=expected)
        )


async def _prepare(directory: Path) -> tuple[BuiltinConfig, ConnectorBinding]:
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{directory / 'pi.db'}"))
    async with open_builtin_runtime(config) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(ScopeDraft(title="Pi recovery", summary="Evidence", idempotency_key="pi"))
        await runtime.ingestion.register(manifest_for_definition(PI_EVENT_SOURCE_DEFINITION))
    binding = ConnectorBinding(
        scope_id=scope.scope_id, binding_id="pi-file", connector_name="pi-session-history", connector_version="1"
    )
    return config, binding


async def _run(
    config: BuiltinConfig, binding: ConnectorBinding, path: Path, leaf: str, *, fail_event=None, reject_event=None
):
    async with open_builtin_runtime(config) as runtime:
        return await ConnectorLifecycle(
            sink=_RuntimeSink(runtime, fail_event=fail_event, reject_event=reject_event),
            checkpoints=_RuntimeCheckpoints(runtime),
        ).run(PiSessionConnector(path, leaf), binding)


async def _snapshot(config: BuiltinConfig, binding: ConnectorBinding):
    async with open_builtin_runtime(config) as runtime:
        checkpoint = (await runtime.ingestion.checkpoint(binding)).checkpoint
    async with open_builtin_contexts(config) as contexts:
        entries = await (await contexts.get(binding.scope_id)).sources.journal.entries()
        captured = {}
        for entry in entries:
            assert isinstance(entry.source, SourceObservation)
            captured[entry.source_ref.source_id] = entry.source
    return checkpoint, captured


def test_partial_write_replay_is_idempotent_and_rewrites_do_not_overwrite_evidence(tmp_path: Path) -> None:
    async def scenario() -> None:
        config, binding = await _prepare(tmp_path)
        path = tmp_path / "session.jsonl"
        records = [_message("root", None, "root evidence")]
        _write(path, records)
        first = await _run(config, binding, path, "root")
        records.extend([_message("one", "root", "original one"), _message("two", "one", "original two")])
        _write(path, records)
        with pytest.raises(OSError, match="injected"):
            await _run(config, binding, path, "two", fail_event="two")
        partial = await _snapshot(config, binding)
        assert partial[0] == first.committed_checkpoint and len(partial[1]) == 2

        original = records[1]
        records[1] = _message("one", "root", "rewritten after partial failure")
        _write(path, records)
        with pytest.raises(ValueError, match="conflicts"):
            await _run(config, binding, path, "two")
        assert await _snapshot(config, binding) == partial

        records[1] = original
        _write(path, records)
        repaired = await _run(config, binding, path, "two")
        assert repaired.status == ConnectorRunStatus.COMPLETE and len(repaired.items) == 2
        assert len((await _snapshot(config, binding))[1]) == 3
        assert (await _run(config, binding, path, "two")).items == ()

    asyncio.run(scenario())


def test_bad_archive_and_rejected_submission_leave_the_durable_checkpoint_unchanged(tmp_path: Path) -> None:
    async def scenario() -> None:
        config, binding = await _prepare(tmp_path)
        path = tmp_path / "session.jsonl"
        records = [_message("root", None, "root")]
        _write(path, records)
        await _run(config, binding, path, "root")
        baseline = await _snapshot(config, binding)
        records.append(_message("new", "root", "new evidence"))
        _write(path, records)
        path.write_bytes(path.read_bytes() + b"{malformed}\n")
        with pytest.raises(ValueError):
            await _run(config, binding, path, "new")
        assert await _snapshot(config, binding) == baseline

        _write(path, records)
        rejected = await _run(config, binding, path, "new", reject_event="new")
        assert rejected.status == ConnectorRunStatus.INCOMPLETE
        assert rejected.items[0].status == ConnectorSubmissionStatus.REJECTED
        assert rejected.committed_checkpoint == baseline[0]
        assert await _snapshot(config, binding) == baseline
        assert len((await _run(config, binding, path, "new")).items) == 1

    asyncio.run(scenario())


def test_inactive_branch_rewrites_and_removals_are_checked_before_new_submissions(tmp_path: Path) -> None:
    async def scenario() -> None:
        config, binding = await _prepare(tmp_path)
        path = tmp_path / "session.jsonl"
        records = [
            _message("root", None, "shared"),
            _message("left", "root", "left"),
            _message("right", "root", "right"),
        ]
        _write(path, records)
        await _run(config, binding, path, "left")
        await _run(config, binding, path, "right")
        baseline = await _snapshot(config, binding)
        records.append(_message("tail", "right", "tail"))
        original = records[1]
        records[1] = _message("left", "root", "changed inactive branch")
        _write(path, records)
        with pytest.raises(ValueError, match="changed or removed"):
            await _run(config, binding, path, "tail")
        assert await _snapshot(config, binding) == baseline
        _write(path, [record for record in records if record["id"] != "left"])
        with pytest.raises(ValueError, match="changed or removed"):
            await _run(config, binding, path, "tail")
        assert await _snapshot(config, binding) == baseline

        records[1] = original
        _write(path, records)
        added = await _run(config, binding, path, "tail")
        assert len(added.items) == 1 and len((await _snapshot(config, binding))[1]) == 4
        _write(path, records, sort_keys=True)
        replay = await _run(config, binding, path, "tail")
        assert replay.items == () and replay.committed_checkpoint == added.committed_checkpoint

    asyncio.run(scenario())


class _ChunkedEvidence:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        entries = []
        for source in request.sources:
            assert isinstance(source, SourceObservation)
            projection = source.projection(TEXT_EVIDENCE_PROJECTION_KEY)
            assert isinstance(projection, dict) and isinstance(projection["content"], str)
            content = projection["content"]
            entries.extend(
                MemoryEntryInput(kind=f"pi-evidence-{index}", text=content[offset : offset + 1500], sources=(source,))
                for index, offset in enumerate(range(0, len(content), 1500))
            )
        return tuple(entries)


def test_large_tool_result_retains_full_original_source_after_exact_memory_readback(tmp_path: Path) -> None:
    async def scenario() -> None:
        config, binding = await _prepare(tmp_path)
        path = tmp_path / "session.jsonl"
        output = "needle 北京 " + "full tool output 北京\n" * 2000
        record: dict[str, JsonValue] = {
            "type": "message",
            "id": "result",
            "parentId": None,
            "message": {
                "role": "toolResult",
                "toolName": "read",
                "toolCallId": "call",
                "content": [{"type": "text", "text": output}],
                "isError": False,
            },
        }
        _write(path, [record])
        await _run(config, binding, path, "result")
        expected_content = f"toolResult: [tool_result read call] {output}"
        async with open_builtin_runtime(config, candidate_pipeline=_ChunkedEvidence()) as runtime:
            assert runtime.memory is not None
            memory = runtime.memory.for_scope(binding.scope_id)
            assert (await memory.flush()).source_count == 1
            page = await memory.search(SearchMemoryRequest(query="needle", mode="fts", limit=10))
            assert page.hits
            hit = page.hits[0]
            citation = MemoryCitation(
                memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
            )
            found = await memory.get(GetMemoryEntryRequest(citation=citation))
            assert found.entry.text == expected_content[:1500].strip()

        async with open_builtin_runtime(config) as runtime:
            assert runtime.memory is not None
            restored = await runtime.memory.for_scope(binding.scope_id).get(GetMemoryEntryRequest(citation=citation))
            assert restored == found
        _, sources = await _snapshot(config, binding)
        assert len(sources) == 1
        source = sources[restored.entry.sources[0].source_id]
        evidence = source.projection(TEXT_EVIDENCE_PROJECTION_KEY)
        assert isinstance(evidence, dict) and evidence["content"] == expected_content
        assert source.payload["event"] == {
            "session_id": "recovery",
            "event_id": "result",
            "parent_id": None,
            "event_type": "message",
            "role": "toolResult",
            "content": expected_content,
            "record": record,
        }

    asyncio.run(scenario())
