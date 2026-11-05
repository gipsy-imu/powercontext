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

import json

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.prepared_context import (
    PreparedContextBuild,
    PreparedContextBuilder,
    PreparedMemoryCandidates,
)
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

REF = ArtifactRef(family="memory", artifact_id="evidence", revision=7)


def _group(*texts: str, scope_id: str = "scope:local") -> PreparedMemoryCandidates:
    return PreparedMemoryCandidates(
        scope_id=scope_id,
        memory_ref=REF,
        hits=tuple(
            MemoryHit(
                memory_ref=REF,
                entry_id=f"entry-{index}",
                entry_version_id=f"version-{index}",
                text=text,
                score=1.0,
                matched_by=("fts",),
            )
            for index, text in enumerate(texts)
        ),
    )


def _assert_wire(build: PreparedContextBuild, groups: tuple[PreparedMemoryCandidates, ...], budget: int) -> None:
    content = build.context.content or ""
    assert build.context.content_bytes == len(content.encode("utf-8")) <= budget
    if not content:
        assert build.context.status == "empty" and not build.fragments and not build.origins
        return
    assert content.startswith(TRUST_POLICY + "\n\nBEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n")
    assert content.endswith("\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1")
    envelope = json.loads(content.splitlines()[-2])
    assert envelope["trust"] == "untrusted_history"
    assert len(envelope["items"]) == len(build.fragments) == len(build.origins)
    for item, fragment, origin in zip(envelope["items"], build.fragments, build.origins, strict=True):
        address = fragment.address
        group = next(group for group in groups if group.scope_id == address.memory.scope_id)
        hit = next(hit for hit in group.hits if hit.entry_id == address.entry_id)
        assert address.memory.artifact == hit.memory_ref and address.entry_version_id == hit.entry_version_id
        assert type(fragment.start) is int and type(fragment.end) is int
        assert 0 <= fragment.start < fragment.end <= len(hit.text)
        assert fragment.text == hit.text[fragment.start : fragment.end]
        assert origin == address
        assert item == {
            "citation": {
                "scope_id": group.scope_id,
                "memory_ref": REF.model_dump(mode="json"),
                "entry_id": hit.entry_id,
                "entry_version_id": hit.entry_version_id,
            },
            "content": fragment.text,
            "start": fragment.start,
            "end": fragment.end,
        }


def test_query_selects_distinct_late_passages_and_literal_offsets() -> None:
    first = "Administrative record. " * 150 + "The nickel probe calibration uses a 43 degree bath."
    second = "Unrelated instrument inventory. " * 100 + "The violet valve releases pressure at 23 degrees."
    groups = (_group(first, second),)
    builder = PreparedContextBuilder()
    for query, text in (("nickel probe calibration", first), ("violet valve pressure", second)):
        request = PrepareContextRequest(query=query, max_bytes=1200, evidence_selection=True)
        build = builder.build_scopes_result(request=request, current_scope_id="scope:local", memory_candidates=groups)
        _assert_wire(build, groups, 1200)
        assert len(build.fragments) == 1
        fragment = build.fragments[0]
        assert fragment.start == text.rindex("The ") and fragment.end == len(text)
        assert (
            builder.build_scopes_result(request=request, current_scope_id="scope:local", memory_candidates=groups)
            == build
        )


def test_one_entry_can_supply_multiple_separated_passages_and_adjacent_sentences() -> None:
    first = "Cobalt calibration records remain exact. Cobalt calibration requires two measurements."
    last = "Amber valve pressure is 19 units."
    text = first + " " + "Routine inventory notes. " * 80 + last
    groups = (_group(text),)
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt calibration amber valve", max_bytes=1400, evidence_selection=True),
        current_scope_id="scope:local",
        memory_candidates=groups,
    )
    _assert_wire(build, groups, 1400)
    assert len(build.fragments) == 2
    assert build.fragments[0].text == first and build.fragments[1].text == last
    assert build.fragments[0].address == build.fragments[1].address


@pytest.mark.parametrize("budget", [512, 650, 800, 1000, 1400, 32768])
def test_utf8_json_escaping_and_envelope_all_count_toward_budget(budget: int) -> None:
    text = '海洋 station "temperature" \\ 原始 e\u0301 🚀\x00\u0085\u2028\u2029 values stay unchanged. ' * 30
    groups = (_group(text),)
    request = PrepareContextRequest(query="海洋 station temperature", max_bytes=budget, evidence_selection=True)
    builder = PreparedContextBuilder()
    build = builder.build_scopes_result(request=request, current_scope_id="scope:local", memory_candidates=groups)
    _assert_wire(build, groups, budget)
    if budget >= 1000:
        assert build.fragments
    probe = builder.probe_budget(request=request, current_scope_id="scope:local", memory_candidates=groups)
    assert probe.delivered_items == len(build.fragments)
    assert probe.unused_bytes == budget - build.context.content_bytes


def test_identical_text_across_scopes_keeps_each_exact_identity() -> None:
    groups = (
        _group("Copper calibration evidence.", scope_id="local"),
        _group("Copper calibration evidence.", scope_id="shared"),
    )
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="copper calibration", evidence_selection=True),
        current_scope_id="local",
        memory_candidates=groups,
    )
    _assert_wire(build, groups, 8000)
    assert [fragment.address.memory.scope_id for fragment in build.fragments] == ["local", "shared"]


def test_long_unpunctuated_text_retains_a_query_passage_near_its_end() -> None:
    text = "office inventory " * 300 + " zirconium sensor calibration value 31 " + "office inventory " * 20
    groups = (_group(text),)
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="zirconium sensor calibration", max_bytes=1000, evidence_selection=True),
        current_scope_id="scope:local",
        memory_candidates=groups,
    )
    _assert_wire(build, groups, 1000)
    assert any("zirconium sensor calibration" in fragment.text for fragment in build.fragments)


def test_closed_mode_matches_ordinary_output_and_empty_selection_is_legal() -> None:
    builder = PreparedContextBuilder()
    groups = (_group("The calibration procedure is unchanged."),)
    ordinary = builder.build_scopes_result(
        request=PrepareContextRequest(query="calibration"), current_scope_id="scope:local", memory_candidates=groups
    )
    explicit = builder.build_scopes_result(
        request=PrepareContextRequest(query="calibration", evidence_selection=False),
        current_scope_id="scope:local",
        memory_candidates=groups,
    )
    assert ordinary == explicit and ordinary.context.content and not ordinary.fragments
    empty = builder.build_scopes_result(
        request=PrepareContextRequest(query="calibration", evidence_selection=True), current_scope_id="scope:local"
    )
    _assert_wire(empty, (), 8000)


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false"])
def test_selection_flag_requires_a_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        PrepareContextRequest.model_validate({"query": "calibration", "evidence_selection": value})


def test_selection_preserves_candidate_reference_validation() -> None:
    group = _group("Calibration record.")
    invalid = PreparedMemoryCandidates(
        scope_id=group.scope_id, hits=group.hits, memory_ref=REF.model_copy(update={"revision": 8})
    )
    with pytest.raises(PreparedContextInvariantError, match="memory-ref-mismatch"):
        PreparedContextBuilder().build_scopes_result(
            request=PrepareContextRequest(query="calibration", evidence_selection=True),
            current_scope_id=group.scope_id,
            memory_candidates=(invalid,),
        )


def test_probe_does_not_report_irrelevant_candidates_as_budget_loss() -> None:
    groups = (_group("Copper calibration evidence.", "Routine office inventory."),)
    request = PrepareContextRequest(query="copper calibration", evidence_selection=True)
    builder = PreparedContextBuilder()
    probe = builder.probe_budget(request=request, current_scope_id="scope:local", memory_candidates=groups)
    assert probe.delivered_items == 1 and probe.dropped_items == 0
