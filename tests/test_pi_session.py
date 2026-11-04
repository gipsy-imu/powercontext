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

import pytest
from pydantic import JsonValue

from powercontext.sources import SourceDefinitionRegistry, SourceMaterialization, TextEvidence
from powercontext.sources.pi_session import PI_EVENT_SOURCE_DEFINITION, PiEventInput, PiSessionInput, parse_pi_session
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def _archive(records: list[dict[str, JsonValue]], *, session_id: str = "s", leaf: str | None = None) -> PiSessionInput:
    header = {"type": "session", "version": 3, "id": session_id}
    content = "\n".join(json.dumps(record, ensure_ascii=False) for record in [header, *records]).encode()
    return PiSessionInput(content=content, leaf_id=leaf)


def _message(event_id: str, parent: str | None, text: str) -> dict[str, JsonValue]:
    return {"type": "message", "id": event_id, "parentId": parent, "message": {"role": "user", "content": text}}


def test_original_evidence_is_not_replaced_by_compaction_or_context_edits() -> None:
    records: list[dict[str, JsonValue]] = [
        _message("root", None, "Original evidence"),
        {
            "type": "compaction",
            "id": "compact",
            "parentId": "root",
            "firstKeptEntryId": "compact",
            "summary": "Summary",
        },
        {"type": "context_edit", "id": "edit", "parentId": "compact", "targetId": "root", "replacement": None},
        _message("branch-a", "edit", "Branch A"),
        _message("other-root", None, "Independent root"),
    ]
    events = parse_pi_session(_archive(records, leaf="branch-a"))
    assert [event.event_id for event in events] == ["root", "compact", "branch-a"]
    assert [event.content for event in events] == ["user: Original evidence", "compaction: Summary", "user: Branch A"]
    assert events[-1].parent_id == "edit"
    assert [event.event_id for event in parse_pi_session(_archive(records))] == ["other-root"]
    assert [event.event_id for event in parse_pi_session(_archive(records, leaf="edit"))] == ["root", "compact"]


def test_block_order_unicode_arguments_and_raw_text_are_preserved() -> None:
    record: dict[str, JsonValue] = {
        "type": "message",
        "id": "mixed",
        "parentId": None,
        "extra": {"latency": 0, "success": False},
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": " first \r\nsecond "},
                {"type": "thinking", "thinking": "omit this"},
                {"type": "toolCall", "name": "read", "id": "call", "arguments": {"z": "北京", "a": [False, None, 2]}},
                {"type": "image", "data": "omit this image"},
                {"type": "text", "text": " last "},
            ],
        },
    }
    event = parse_pi_session(_archive([record]))[0]
    assert (
        event.content == 'assistant:  first \r\nsecond \n[tool_call read call] {"a":[false,null,2],"z":"北京"}\n last '
    )
    assert event.record == record


def test_unicode_line_separators_inside_text_do_not_split_jsonl_records() -> None:
    events = parse_pi_session(_archive([_message("a", None, "before\u2028middle\u2029after")]))
    assert events[0].content == "user: before\u2028middle\u2029after"


def test_only_invisible_content_is_ignored_and_control_leaf_preserves_ancestors() -> None:
    events = parse_pi_session(
        _archive([
            _message("a", None, "  raw whitespace  "),
            {
                "type": "message",
                "id": "b",
                "parentId": "a",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "hidden"}, {"type": "image", "data": "hidden"}],
                },
            },
            {
                "type": "message",
                "id": "c",
                "parentId": "b",
                "message": {"role": "system", "content": "system configuration"},
            },
            {"type": "model_change", "id": "d", "parentId": "c", "modelId": "offline"},
        ])
    )
    assert [event.event_id for event in events] == ["a"]
    assert events[0].content == "user:   raw whitespace  "


@pytest.mark.parametrize(
    "records",
    [
        [_message("a", None, "valid"), {"type": "model_change", "id": "b", "parentId": "missing"}],
        [
            _message("a", None, "valid"),
            {"type": "model_change", "id": "b", "parentId": "later"},
            _message("later", "a", "future"),
        ],
        [_message("a", None, "valid"), {"type": "model_change", "id": "b", "parentId": "b"}],
        [_message("a", None, "valid"), _message("a", None, "duplicate")],
        [_message("a", None, "valid"), {"type": "model_change", "id": "b"}],
        [_message("a", None, "valid"), {"type": "model_change", "id": "b", "parentId": 1}],
        [_message("a", None, "valid"), {"type": "model_change", "id": 7, "parentId": None}],
        [_message("a", None, "valid"), {"type": "model_change", "id": "", "parentId": None}],
        [_message("a", None, "valid"), {"type": "session", "id": "second-header", "parentId": None}],
    ],
)
def test_unselected_invalid_records_still_fail_atomic_validation(records: list[dict[str, JsonValue]]) -> None:
    with pytest.raises(ValueError):
        parse_pi_session(_archive(records, leaf="a"))


@pytest.mark.parametrize(
    "tail",
    [
        b'{"type":"model_change","id":"b","parentId":null,"value":NaN}',
        b'{"type":"model_change","id":"b","parentId":null,"value":1e9999}',
        b'{"type":"model_change","id":"b","parentId":null,"value":"\\ud800"}',
        b'{"type":"model_change","id":"b","parentId":null,"value":"\xff"}',
        b'{"unfinished":',
        b"[1,2]",
    ],
)
def test_invalid_json_after_a_valid_selected_leaf_returns_no_partial_events(tail: bytes) -> None:
    value = _archive([_message("a", None, "valid")], leaf="a")
    with pytest.raises(ValueError):
        parse_pi_session(value.model_copy(update={"content": value.content + b"\n" + tail}))


@pytest.mark.parametrize("version", [None, "3", 3.0, True, 2, 4])
def test_header_version_must_be_integer_three(version: JsonValue) -> None:
    raw = json.dumps({"type": "session", "version": version, "id": "s"}).encode()
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=raw))


def test_header_only_has_no_events_and_an_explicit_missing_leaf_fails() -> None:
    assert parse_pi_session(_archive([])) == ()
    with pytest.raises(ValueError):
        parse_pi_session(_archive([], leaf="absent"))


def test_source_identity_is_bounded_distinct_and_independent_of_text() -> None:
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
    pairs = [("a:b", "c"), ("a", "b:c"), ("a%3Ab", "c"), ("表" * 500, "事" * 500)]
    names = set()
    for session_id, event_id in pairs:
        first = parse_pi_session(_archive([_message(event_id, None, "first")], session_id=session_id))[0]
        changed = parse_pi_session(_archive([_message(event_id, None, "changed")], session_id=session_id))[0]
        source = asyncio.run(registry.resolve(first))
        assert source.materialization == SourceMaterialization.CAPTURED
        assert 0 < len(source.name) <= 256
        assert source.name == asyncio.run(registry.resolve(changed)).name
        names.add(source.name)
    assert len(names) == len(pairs)


def test_read_and_projection_preserve_complete_record_and_do_not_alias_input() -> None:
    event = parse_pi_session(_archive([_message("a", None, "captured")]))[0]
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(event))
    expected = event.model_copy(deep=True)
    event.record["annotation"] = "changed outside the capture"
    readback = asyncio.run(registry.read(source))
    assert isinstance(readback, PiEventInput)
    assert readback == expected
    readback.record["annotation"] = "changed by a consumer"
    assert asyncio.run(registry.read(source)) == expected
    evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence == TextEvidence(
        source_type="pi-event",
        source_id=source.name,
        content="user: captured",
        metadata={"session_id": "s", "event_id": "a", "parent_id": None, "event_type": "message", "role": "user"},
    )
