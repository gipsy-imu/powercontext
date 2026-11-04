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

from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceMaterialization,
    TextEvidence,
)
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)


def test_capture_and_json_roundtrip_preserve_every_field_in_header_order() -> None:
    content = ' z ,ID,id,number,boolean,empty,note\r\n001,UP, key ,0001.00,false,,"北京\r\nA"\r\n'.encode()
    row = parse_csv_document(CSVDocumentInput(document_id="表/记录", primary_key="id", content=content))[0]
    assert row.columns == (" z ", "ID", "id", "number", "boolean", "empty", "note")
    assert row.values == ("001", "UP", " key ", "0001.00", "false", "", "北京\r\nA")
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(row))
    assert isinstance(source, CSVRowSource)
    restored = CSVRowSource.model_validate_json(source.model_dump_json())
    assert restored.materialization == SourceMaterialization.CAPTURED
    assert asyncio.run(registry.read(restored)) == row
    evidence = TextEvidence.model_validate(registry.project(restored, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence.content == (
        '{" z ":"001","ID":"UP","id":" key ","number":"0001.00","boolean":"false","empty":"","note":"北京\\r\\nA"}'
    )
    assert list(evidence.metadata) == ["document_id", "primary_key", "primary_key_value", "columns", "row"]
    assert list(json.loads(evidence.content)) == list(row.columns)
    assert evidence.metadata["row"] == dict(zip(row.columns, row.values, strict=True))


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_empty_physical_records_are_ignored_but_empty_nonkey_values_are_retained(newline: str) -> None:
    content = newline.join(("id,value", "", "01,", "", '02,""', "")).encode()
    rows = parse_csv_document(CSVDocumentInput(document_id="empty-values", primary_key="id", content=content))
    assert [row.values for row in rows] == [("01", ""), ("02", "")]


@pytest.mark.parametrize(
    "content",
    [
        b"\nid\n1\n",  # The first physical record cannot be skipped as a header.
        b'id\n1\n""\n',  # Quoted empty data is a record, not an empty physical line.
        b"id\n1\n \t\n",
        b'id,value\n1,valid\n2,"unterminated',
        b'id,value\n1,valid\n2,"closed"trailing\n',
        b"id,value\n1,valid\n2,too,many\n",
        b"id,value\n1,valid\n1,duplicate-at-end\n",
        b"id,value\n1,valid\n2,\xff\n",
    ],
)
def test_document_errors_after_valid_rows_fail_the_whole_parse(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="atomic", primary_key="id", content=content))


def test_standard_csv_unquoted_quotes_and_space_after_delimiter_are_preserved() -> None:
    rows = parse_csv_document(
        CSVDocumentInput(document_id="quotes", primary_key="id", content=b'id,value\n1, a"b\n2,"a""b"\n')
    )
    assert [row.values for row in rows] == [("1", ' a"b'), ("2", 'a"b')]


def test_identity_encodes_components_separately_and_does_not_normalize_unicode() -> None:
    pairs = [("a:b", "c"), ("a", "b:c"), ("a%3Ab", "c"), ("a/b", "c"), ("é", "c"), ("e\u0301", "c")]
    identities = [csv_row_source_id(document_id, key) for document_id, key in pairs]
    assert len(set(identities)) == len(pairs)
    assert identities[:4] == ["csv:a%3Ab:c", "csv:a:b%3Ac", "csv:a%253Ab:c", "csv:a%2Fb:c"]
    assert csv_row_source_id("reserved", " /%:#? ") == "csv:reserved:%20%2F%25%3A%23%3F%20"


@pytest.mark.parametrize("key", ["a" * 250, "é" * 41 + "aaaa"])
def test_identity_length_limit_is_applied_after_percent_encoding(key: str) -> None:
    assert len(csv_row_source_id("d", key)) == 256
    with pytest.raises(ValueError):
        csv_row_source_id("d", key + "a")
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="d", primary_key="id", content=f"id\n{key}a\n".encode()))


@pytest.mark.parametrize(
    ("document_id", "key"),
    [("", "1"), ("\t", "1"), (" d", "1"), ("d\u2003", "1"), ("d", ""), ("d", "\n\t")],
)
def test_invalid_identity_components_raise_value_error(document_id: str, key: str) -> None:
    with pytest.raises(ValueError):
        csv_row_source_id(document_id, key)


@pytest.mark.parametrize(
    "changes",
    [
        {"primary_key": " id "},
        {"primary_key_value": "different"},
        {"columns": ("id", "id")},
        {"columns": ("id", " ")},
        {"values": ("1",)},
        {"document_id": " d "},
    ],
)
def test_direct_row_input_obeys_the_same_record_contract(changes: dict[str, object]) -> None:
    payload = {
        "document_id": "d",
        "primary_key": "id",
        "primary_key_value": "1",
        "columns": ("id", "value"),
        "values": ("1", "original"),
    }
    with pytest.raises(ValueError):
        CSVRowInput.model_validate(payload | changes)
