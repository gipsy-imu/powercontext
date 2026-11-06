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
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import CapabilityNotSupportedError, EmbeddingProfile, MemorySearchRequest
from powercontext.builtin.persistence.seekdb.memory_index import SeekDBMemoryIndex
from powercontext.builtin.tags import TagFilter

PROFILE = EmbeddingProfile(profile_id="native-test", model="fixed", dimension=3, distance="l2", normalization="unit")
HEAD = ArtifactRef(family="memory", artifact_id="records", revision=9)


class _Rows:
    def mappings(self):
        return self

    def all(self):
        # Incremental projections deliberately retain an older stamping revision.
        return [
            {
                "channel": channel,
                "memory_artifact_id": "records",
                "head_revision": 2,
                "entry_id": "entry-a",
                "entry_version_id": "version-b",
                "text": "Original calibration record.",
                "distance": None if channel == "fts" else 0.125,
            }
            for channel in ("fts", "vector")
        ]


class _Connection:
    def __init__(self):
        self.executed = []

    async def execute(self, statement, parameters):
        self.executed.append((statement, parameters))
        return _Rows()


def _request(**updates):
    return MemorySearchRequest.model_validate({
        "query": "calibration' OR 1=1 --",
        "analyzed_query": "calibration or 1 1",
        "memories": (HEAD,),
        "candidate_limit": 4,
        "mode": "hybrid",
        "query_vector": (1.0, 0.0, 0.0),
        "embedding_profile": PROFILE,
        **updates,
    })


@pytest.mark.parametrize(
    "tag_filter", [None, TagFilter(tags=("selected",)), TagFilter(tags=("selected", "reviewed"), match="any")]
)
def test_hybrid_uses_one_bound_statement_and_current_requested_reference(monkeypatch, tag_filter):
    async def scenario():
        index = SeekDBMemoryIndex(PROFILE)
        monkeypatch.setattr(index, "vector_complete", AsyncMock(return_value=True))
        connection = _Connection()
        request = _request(tag_filter=tag_filter)
        channels = await index.search(cast(AsyncConnection, connection), "project:one", request)
        assert len(connection.executed) == 1
        statement, parameters = connection.executed[0]
        assert parameters["scope_id"] == "project:one"
        assert parameters["memory_artifact_ids"] == (HEAD.artifact_id,)
        assert parameters["candidate_limit"] == 4
        assert parameters["query_vector"] == request.query_vector
        assert parameters["analyzed_query"] == request.analyzed_query
        assert "calibration" not in str(statement)
        assert ("tag_keys" in parameters) == (tag_filter is not None)
        if tag_filter is not None:
            assert parameters["tag_keys"] == tag_filter.keys
        assert len(channels.fts) == len(channels.vector) == 1
        assert channels.fts[0].memory_ref == channels.vector[0].memory_ref == HEAD
        assert channels.fts[0].entry_version_id == channels.vector[0].entry_version_id == "version-b"
        assert channels.vector[0].distance == 0.125 and channels.fts[0].distance is None
        assert channels.vector[0].text == channels.fts[0].text == "Original calibration record."

    asyncio.run(scenario())


@pytest.mark.parametrize("updates", [{"query_vector": None}, {"embedding_profile": None}])
def test_invalid_hybrid_profile_is_rejected_before_candidate_query(updates):
    async def scenario():
        connection = _Connection()
        with pytest.raises(CapabilityNotSupportedError):
            await SeekDBMemoryIndex(PROFILE).search(cast(AsyncConnection, connection), "scope", _request(**updates))
        assert not connection.executed

    asyncio.run(scenario())


def test_incomplete_vectors_remain_an_explicit_capability_failure(monkeypatch):
    async def scenario():
        index = SeekDBMemoryIndex(PROFILE)
        monkeypatch.setattr(index, "vector_complete", AsyncMock(return_value=False))
        connection = _Connection()
        with pytest.raises(CapabilityNotSupportedError):
            await index.search(cast(AsyncConnection, connection), "scope", _request())
        assert not connection.executed

    asyncio.run(scenario())
