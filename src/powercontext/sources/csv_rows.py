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

"""Row-level CSV Sources with stable identity and canonical text evidence.

Each physical CSV record becomes one CAPTURED Source whose identity depends only
on ``document_id`` and the primary key value. Row order and line numbers are
deliberately excluded: a retried import, a reordered file or a restarted process
must all resolve to the same identity.

Values are kept as the exact strings that appeared in the document. No trimming,
no numeric/date/boolean coercion, and no column-name normalisation happens here,
because the challenge requires the projection to be byte-faithful to the input.
"""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME = "csv-row"
# Identities are ``csv:{document}:{key}``; the two literal separators cost 5 bytes.
_IDENTITY_OVERHEAD = len("csv::")


class CSVDocumentInput(BaseModel):
    """One complete offline CSV document supplied as raw bytes."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One parsed CSV row, preserving header order and raw string values."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """A captured Source holding exactly one CSV row."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Return the stable Source identity for one row.

    Both components are percent-encoded with ``safe=""`` so that a ``:`` or ``/``
    inside a document id or key can never be confused with the literal separators
    of the identity itself. Without that, ``doc:one`` + ``key`` and ``doc`` +
    ``one:key`` would collide on the same string.
    """

    _require_canonical(document_id, "document_id")
    if not isinstance(primary_key_value, str) or not primary_key_value.strip():
        raise ValueError("primary_key_value must be a non-blank string")

    identity = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(
            f"csv row source identity must not exceed {MAX_SOURCE_ID_LENGTH} characters, got {len(identity)}"
        )
    return identity


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse one CSV document into rows, or fail without publishing partial output.

    The whole document is materialised and validated before anything is returned.
    A generator that yields as it reads would leak the first N rows when row N+1
    is invalid, which is exactly what the atomicity requirement forbids.
    """

    _require_canonical(value.document_id, "document_id")
    if not isinstance(value.primary_key, str) or not value.primary_key.strip():
        raise ValueError("primary_key must be a non-blank string")

    # utf-8-sig strips a leading BOM when present and is a no-op otherwise, so a
    # BOM-prefixed header still compares exactly as the caller wrote it.
    try:
        text = value.content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content must be valid UTF-8") from error

    # newline="" keeps embedded CRLF inside quoted fields intact; strict=True turns
    # malformed quoting (an unterminated quote) into an error instead of silent data.
    try:
        records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as error:
        # csv.Error does not inherit from ValueError, so it must be translated to
        # keep the documented "ValueError on any format error" contract.
        raise ValueError(f"malformed CSV content: {error}") from error

    if not records:
        raise ValueError("CSV document must contain a header row")

    header = tuple(records[0])
    seen_columns: set[str] = set()
    for column in header:
        # A blank header field means the row has no addressable name; a duplicate
        # one makes ``row[column]`` ambiguous. Both are rejected rather than fixed.
        if not column.strip():
            raise ValueError("CSV header fields must be non-blank")
        if column in seen_columns:
            raise ValueError(f"duplicate CSV header field: {column!r}")
        seen_columns.add(column)

    if value.primary_key not in seen_columns:
        raise ValueError(f"primary_key {value.primary_key!r} does not match any CSV header field")

    rows: list[CSVRowInput] = []
    seen_keys: set[str] = set()
    for record in records[1:]:
        # csv.reader reports a physically empty line as an empty list, not as a
        # row of empty strings, so this is the only "ignore" case the spec allows.
        if not record:
            continue
        if len(record) != len(header):
            raise ValueError(f"CSV record width {len(record)} does not match header width {len(header)}")

        mapping = dict(zip(header, record, strict=True))
        primary_key_value = mapping[value.primary_key]
        if not primary_key_value.strip():
            raise ValueError("primary key values must not be blank")
        if primary_key_value in seen_keys:
            raise ValueError(f"duplicate primary key value: {primary_key_value!r}")
        seen_keys.add(primary_key_value)

        rows.append(
            CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=primary_key_value,
                columns=header,
                values=tuple(record),
            )
        )

    return tuple(rows)


class CSVRowSourceAdapter:
    """Resolve and read row-level CSV Sources."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            # name and source_id are intentionally the same value: the registry
            # validates ``name`` and consumers address the row by ``source_id``.
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Expose one row as canonical text evidence without leaking the Source class."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        # dict(zip(...)) preserves the header order, so the JSON object below has
        # the same key order as the CSV columns.
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            # Compact separators and ensure_ascii=False are part of the contract:
            # the default ``", "`` spacing would not equal the expected string.
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    CSVRowSourceAdapter(),
    projections=(CSVRowTextEvidenceProjection(),),
)


def _require_canonical(value: object, field: str) -> None:
    """Reject blank or whitespace-padded identity components.

    Surrounding whitespace is meaningful in this challenge, so ``" doc "`` and
    ``"doc"`` must never be silently treated as the same document.
    """

    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field} must not contain leading or trailing whitespace")
