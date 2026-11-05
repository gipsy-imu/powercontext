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

import pytest
from pydantic import ValidationError

from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleEvent


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("node_id", ""),
        ("node_id", " \n"),
        ("node_id", 17),
        ("reason", ""),
        ("reason", "\t"),
        ("reason", None),
        ("revision", True),
        ("revision", "3"),
        ("revision", 3.0),
        ("revision", 0),
        ("sequence", False),
        ("sequence", "1"),
        ("sequence", 1.0),
        ("sequence", -1),
        ("available", "false"),
        ("available", 1),
        ("available", None),
        ("extra", "unexpected"),
    ],
)
def test_events_reject_coercion_blank_fields_and_unknown_fields(field, invalid):
    fields = {"node_id": "root", "revision": 3, "sequence": 1, "available": False, "reason": "withdrawn"}
    fields[field] = invalid
    with pytest.raises(ValidationError):
        SourceLifecycleEvent.model_validate(fields)


def test_event_roundtrip_preserves_identifiers_and_reasons_exactly():
    event = SourceLifecycleEvent(node_id=" evidence ", revision=3, sequence=17, available=True, reason=" 恢复资料 ")
    decoded = SourceLifecycleEvent.model_validate_json(event.model_dump_json())
    assert decoded == event
    assert decoded.node_id == " evidence " and decoded.reason == " 恢复资料 "
