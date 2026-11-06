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
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import Engine, event

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput, MemorySearchRequest
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.persistence.seekdb.memory_index import SeekDBMemoryIndex
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts
from powercontext.builtin.tags import MemoryEntryTagTarget, TagFilter

PROFILE = EmbeddingProfile(profile_id="hybrid-ties", model="fixed", dimension=4, distance="l2", normalization="unit")


class _Embedding:
    profile = PROFILE

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(vectors=((1.0, 0.0, 0.0, 0.0),) * len(texts))


@pytest.mark.skipif(
    importlib.util.find_spec("pylibseekdb") is None, reason="requires the optional embedded seekdb binding"
)
def test_native_hybrid_limits_tags_multiple_memories_and_ties(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = BuiltinConfig(database=SeekDBConfig(path=tmp_path / "db"))
        async with open_builtin_contexts(config, embedding_model=_Embedding()) as contexts:
            memory = (await contexts.get("project")).artifacts.memory
            first = await memory.remember(
                memory=None,
                entries=tuple(
                    MemoryEntryInput(kind="fact", text=f"Calibration pressure reading {index}.") for index in range(18)
                ),
                mode="append",
            )
            created = await contexts.records.create_artifact(
                "project",
                "memory",
                ArtifactWrite(
                    content={
                        "entries": [
                            {"kind": "fact", "text": f"Calibration temperature reading {index}."} for index in range(12)
                        ]
                    }
                ),
            )
            second = await memory.head(created.artifact_id)
            assert first is not None and second is not None and first.artifact_id != second.artifact_id
            latest = await memory.remember(
                memory=first,
                entries=(MemoryEntryInput(kind="fact", text="Calibration additional reading."),),
                mode="append",
            )
            assert latest is not None
            other = (await contexts.get("elsewhere")).artifacts.memory
            await other.remember(
                memory=None, entries=(MemoryEntryInput(kind="fact", text="Calibration decoy."),), mode="append"
            )
            tagged = [*list(first.content.manifest.entries)[:3], *list(second.content.manifest.entries)[:2]]
            selected_ids = {entry.entry_id for entry in tagged}
            for position, entry in enumerate(tagged):
                artifact_id = first.artifact_id if position < 3 else second.artifact_id
                target = MemoryEntryTagTarget(artifact_id=artifact_id, entry_id=entry.entry_id)
                current = await contexts.records.get_tags("project", target)
                await contexts.records.replace_tags(
                    "project", target, ("selected", "common"), expected_etag=current.etag
                )

            index = SeekDBMemoryIndex(PROFILE)
            request = MemorySearchRequest(
                query="calibration",
                analyzed_query="calibration",
                memories=(latest.as_ref(), second.as_ref()),
                candidate_limit=3,
                mode="hybrid",
                query_vector=(1.0, 0.0, 0.0, 0.0),
                embedding_profile=PROFILE,
            )
            statements = []

            def capture(_connection, _cursor, statement, parameters, _context, _many):
                if "pc_memory_" in statement and ("AGAINST" in statement.upper() or "l2_distance" in statement.lower()):
                    statements.append((statement, parameters))

            event.listen(Engine, "before_cursor_execute", capture)
            try:
                async with contexts.database.transaction() as connection:
                    original = await index.search(connection, "project", request)
                    assert len(statements) == 1 and len(original.fts) == len(original.vector) == 3
                    assert all(hit.memory_ref in request.memories for hit in (*original.fts, *original.vector))
                    for _ in range(3):
                        assert await index.search(connection, "project", request) == original
                    tagged_request = request.model_copy(update={"tag_filter": TagFilter(tags=("selected", "common"))})
                    count = len(statements)
                    selected = await index.search(connection, "project", tagged_request)
                    assert len(statements) == count + 1
                    assert len(selected.fts) == len(selected.vector) == 3
                    assert all(hit.entry_id in selected_ids for hit in (*selected.fts, *selected.vector))
                    for statement, parameters in (statements[0], statements[-1]):
                        plan = "\n".join(
                            str(row[0])
                            for row in (await connection.exec_driver_sql("EXPLAIN " + statement, parameters)).all()
                        )
                        assert "TEXT RETRIEVAL SCAN" in plan and "VECTOR INDEX" in plan
                    for mode in ("fts", "vector"):
                        count = len(statements)
                        channels = await index.search(connection, "project", request.model_copy(update={"mode": mode}))
                        assert len(statements) == count + 1
                        assert len(channels.fts if mode == "fts" else channels.vector) == 3
                        assert not (channels.vector if mode == "fts" else channels.fts)
                    missing = request.model_copy(update={"tag_filter": TagFilter(tags=("absent",))})
                    empty = await index.search(connection, "project", missing)
                    assert empty.fts == empty.vector == ()
            finally:
                event.remove(Engine, "before_cursor_execute", capture)

    asyncio.run(scenario())
