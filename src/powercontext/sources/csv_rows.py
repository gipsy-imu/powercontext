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

"""Offline CSV records captured as individually addressable evidence."""

from __future__ import annotations

import csv
import io
import json
from typing import Self
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]


def _require_nonblank(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} must not be empty or whitespace")  # noqa: TRY003


def _validate_document_id(value: str) -> None:
    _require_nonblank(value, "document_id")
    if value != value.strip():
        raise ValueError("document_id must not have surrounding whitespace")  # noqa: TRY003


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Encode a document/key pair without changing either component's identity."""
    _validate_document_id(document_id)
    _require_nonblank(primary_key_value, "primary_key_value")
    identity = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"CSV row source identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def _primary_key_index(columns: tuple[str, ...], primary_key: str) -> int:
    _require_nonblank(primary_key, "primary_key")
    if not columns or any(not column.strip() for column in columns):
        raise ValueError("CSV header must contain nonblank column names")  # noqa: TRY003
    if len(set(columns)) != len(columns):
        raise ValueError("CSV header column names must be unique")  # noqa: TRY003
    try:
        return columns.index(primary_key)
    except ValueError as error:
        raise ValueError("primary_key must exactly match a CSV header column") from error  # noqa: TRY003


def _validate_row(
    document_id: str,
    primary_key: str,
    primary_key_value: str,
    columns: tuple[str, ...],
    values: tuple[str, ...],
) -> None:
    csv_row_source_id(document_id, primary_key_value)
    index = _primary_key_index(columns, primary_key)
    if len(values) != len(columns):
        raise ValueError("CSV row width must match its header")  # noqa: TRY003
    if values[index] != primary_key_value:
        raise ValueError("primary_key_value must match the row's primary key field")  # noqa: TRY003


class CSVDocumentInput(BaseModel):
    """One complete UTF-8 CSV document, with an exact primary-key column."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    primary_key: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        _validate_document_id(value)
        return value

    @field_validator("primary_key")
    @classmethod
    def validate_primary_key(cls, value: str) -> str:
        _require_nonblank(value, "primary_key")
        return value


class CSVRowInput(BaseModel):
    """A complete CSV record whose strings and original column order are retained."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        _validate_row(self.document_id, self.primary_key, self.primary_key_value, self.columns, self.values)
        return self


class CSVRowSource(Source):
    """Captured row evidence, independent of the original file and row position."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        _validate_row(self.document_id, self.primary_key, self.primary_key_value, self.columns, self.values)
        identity = csv_row_source_id(self.document_id, self.primary_key_value)
        if self.source_id != identity or self.name != identity:
            raise ValueError("CSV row name and source_id must equal its document/key identity")  # noqa: TRY003
        if self.materialization != SourceMaterialization.CAPTURED:
            raise ValueError("CSV row sources must contain captured evidence")  # noqa: TRY003
        return self


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Validate the entire document before returning any row captures.

    Only physically empty records after the header are skipped. In particular,
    quoted empty fields are data and still undergo width and key validation.
    """
    try:
        text = value.content.decode("utf-8-sig", errors="strict")
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        columns = tuple(next(reader, ()))
        key_index = _primary_key_index(columns, value.primary_key)
        rows: list[CSVRowInput] = []
        seen_keys: set[str] = set()
        for record in reader:
            if not record:
                continue
            if len(record) != len(columns):
                raise ValueError(f"CSV record ending at line {reader.line_num} has the wrong number of fields")  # noqa: TRY003
            key = record[key_index]
            if key in seen_keys:
                raise ValueError(f"CSV record ending at line {reader.line_num} has a duplicate primary key")  # noqa: TRY003
            rows.append(
                CSVRowInput(
                    document_id=value.document_id,
                    primary_key=value.primary_key,
                    primary_key_value=key,
                    columns=columns,
                    values=tuple(record),
                )
            )
            seen_keys.add(key)
        return tuple(rows)
    except (UnicodeError, csv.Error) as error:
        raise ValueError(f"Invalid UTF-8 CSV document: {error}") from error  # noqa: TRY003


class _CSVRowAdapter:
    input_class = CSVRowInput
    name = "csv-row"
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        identity = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            **value.model_dump(),
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class _CSVRowTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type="csv-row",
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    _CSVRowAdapter(),
    version="1",
    projections=(_CSVRowTextEvidenceProjection(),),
)
