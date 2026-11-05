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
from functools import partial

import pytest
from sqlalchemy import event, insert

from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.processing_intents import ArtifactProcessingIntentRepository
from powercontext.builtin.persistence.tables import SOURCE_JOURNAL_HEADS_TABLE
from powercontext.builtin.runtime.artifact_processing import ArtifactProcessingBinding, ArtifactProcessingSupervisor
from powercontext.builtin.runtime.config import RuntimeConfig
from powercontext.builtin.runtime.processing_discovery import source_count_scopes
from powercontext.builtin.sources import SourceCursor
from tests.builtin.runtime.test_processing_scheduler import _intent, _Launcher, _profile, _request, _wait


def test_count_page_uses_the_correct_family_cursor(tmp_path):
    async def scenario():
        async with _profile(tmp_path, "count-cursors") as profile:
            async with profile.database.transaction() as connection:
                await connection.execute(
                    insert(SOURCE_JOURNAL_HEADS_TABLE),
                    [{"scope_id": "consumed", "position": 8}, {"scope_id": "new", "position": 3}],
                )
                await SourceCursorRepository().save(
                    connection, "consumed", "memory-binding", SourceCursor(sequence=6), expected_generation=None
                )
                memory = await source_count_scopes(
                    connection, ("consumed", "new", "missing"), binding_name="memory-binding", threshold=3
                )
                topic = await source_count_scopes(
                    connection, ("consumed", "new"), binding_name="topic-binding", threshold=3
                )
            assert memory == {"new"}
            assert topic == {"consumed", "new"}

    asyncio.run(scenario())


def test_count_admission_reaches_eligible_scopes_after_many_ineligible_pages(tmp_path):
    async def scenario():
        async with _profile(tmp_path, "count-pages") as profile:
            # This exceeds a discovery page without requiring any Source bodies
            # or model construction during scheduling.
            scopes = tuple(f"scope-{index:03}" for index in range(135))
            for scope in scopes:
                await _request(profile.database, "memory-binding", scope, requested=False)
            async with profile.database.transaction() as connection:
                await connection.execute(
                    insert(SOURCE_JOURNAL_HEADS_TABLE),
                    [{"scope_id": scope, "position": 3 if scope == scopes[-1] else 2} for scope in scopes],
                )
            launcher = _Launcher(profile.database)
            async with ArtifactProcessingSupervisor(
                database=profile.database,
                bindings=(
                    ArtifactProcessingBinding(
                        "memory-binding",
                        "memory",
                        launcher,
                        count_scope_filter=partial(source_count_scopes, binding_name="memory-binding", threshold=3),
                    ),
                ),
                lease_mode="single-process",
            ) as supervisor:
                await _wait(lambda: supervisor.family_status["memory"]["completed"] == 1)
                assert [work.scope_id for work in launcher.assignments] == [scopes[-1]]
                for scope in scopes[:-1]:
                    row = await _intent(profile.database, "memory-binding", scope)
                    assert row.requested_generation == row.handled_generation == 0

    asyncio.run(scenario())


class _AdmissionFailure(Exception):
    pass


class _FailingRequests(ArtifactProcessingIntentRepository):
    fail = True

    async def request(self, connection, scope_id, binding_name, /):
        result = await super().request(connection, scope_id, binding_name)
        if self.fail and scope_id == "b":
            self.fail = False
            raise _AdmissionFailure
        return result


@pytest.mark.parametrize("boundary", ["request", "commit"])
def test_failed_count_admission_does_not_lose_or_partially_accept_its_page(tmp_path, boundary):
    async def scenario():
        async with _profile(tmp_path, "count-rollback") as profile:
            for scope in ("a", "b"):
                await _request(profile.database, "memory-binding", scope, requested=False)
            intents = _FailingRequests() if boundary == "request" else ArtifactProcessingIntentRepository()
            launcher = _Launcher(profile.database)

            async def eligible(_connection, scopes):
                return frozenset(scopes)

            supervisor = ArtifactProcessingSupervisor(
                database=profile.database,
                bindings=(
                    ArtifactProcessingBinding("memory-binding", "memory", launcher, count_scope_filter=eligible),
                ),
                lease_mode="single-process",
                intents=intents,
            )
            await supervisor._acquire()
            state = supervisor._families["memory-binding"]

            def fail_commit(_connection):
                raise _AdmissionFailure

            try:
                if boundary == "commit":
                    event.listen(profile.database.engine.sync_engine, "commit", fail_commit, once=True)
                try:
                    with pytest.raises(_AdmissionFailure):
                        await supervisor._discover_count(state)
                finally:
                    if boundary == "commit":
                        event.remove(profile.database.engine.sync_engine, "commit", fail_commit)
                for scope in ("a", "b"):
                    assert (await _intent(profile.database, "memory-binding", scope)).requested_generation == 0
                await supervisor._discover_count(state)
                for scope in ("a", "b"):
                    assert (await _intent(profile.database, "memory-binding", scope)).requested_generation == 1
            finally:
                await supervisor._lose_leadership()

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["memory_source_count_threshold", "topic_memory_source_count_threshold"])
def test_count_thresholds_remain_strict_after_json_configuration_roundtrip(field):
    from pydantic import ValidationError

    assert getattr(RuntimeConfig.model_validate_json('{"' + field + '":3}'), field) == 3
    for literal in ("true", "false", "0", "-1", "3.0", '"3"'):
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate_json('{"' + field + '":' + literal + "}")
