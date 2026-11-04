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

"""Capture immutable visible events from a Pi v3 session's selected history."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from powercontext.errors import SourceConflictError
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
]


def _nonblank_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonblank string")  # noqa: TRY003
    return value


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _source_id(session_id: str, event_id: str) -> str:
    # Encode a pair, rather than joining raw IDs, so delimiters cannot collide.
    identity = _json_text([session_id, event_id]).encode("utf-8")
    return f"pi:{hashlib.sha256(identity).hexdigest()}"


class PiSessionInput(BaseModel):
    """Complete offline Pi JSONL bytes and an optional exact leaf entry ID."""

    model_config = ConfigDict(frozen=True)

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One original visible event, including the complete unmodified record."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        _nonblank_string(self.session_id, "session_id")
        _nonblank_string(self.event_id, "event_id")
        if (
            self.record.get("id") != self.event_id
            or "parentId" not in self.record
            or self.record["parentId"] != self.parent_id
            or self.record.get("type") != self.event_type
        ):
            raise ValueError("event identity must match its original record")  # noqa: TRY003
        if self.parent_id is not None:
            _nonblank_string(self.parent_id, "parent_id")
        if _visible_event(self.record) != (self.role, self.content):
            raise ValueError("event text must match its original record")  # noqa: TRY003
        _json_text(self.record).encode("utf-8")
        return self


def _text_content(value: JsonValue, *, tool_calls: bool = False) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("text blocks must contain string text")  # noqa: TRY003
            parts.append(text)
        elif tool_calls and block.get("type") == "toolCall":
            name = _nonblank_string(block.get("name"), "toolCall.name")
            call_id = _nonblank_string(block.get("id"), "toolCall.id")
            parts.append(f"[tool_call {name} {call_id}] {_json_text(block.get('arguments', {}))}")
    return "\n".join(parts)


def _message_event(record: dict[str, JsonValue]) -> tuple[str, str] | None:
    message = record.get("message")
    if not isinstance(message, dict):
        raise ValueError("message entries must contain a message object")  # noqa: TRY003, TRY004
    role = _nonblank_string(message.get("role"), "message.role")
    if role == "system":
        return None
    if role == "bashExecution":
        command, output = message.get("command", ""), message.get("output", "")
        if not isinstance(command, str) or not isinstance(output, str):
            raise ValueError("bash command and output must be strings")  # noqa: TRY003
        body = f"$ {command}\n{output}" if command or output else ""
    else:
        body = _text_content(message.get("content"), tool_calls=True)
        if role == "toolResult" and body:
            name = _nonblank_string(message.get("toolName"), "toolResult.toolName")
            call_id = _nonblank_string(message.get("toolCallId"), "toolResult.toolCallId")
            body = f"[tool_result {name} {call_id}] {body}"
    return (role, f"{role}: {body}") if body else None


def _visible_event(record: dict[str, JsonValue]) -> tuple[str, str] | None:
    event_type = _nonblank_string(record.get("type"), "entry.type")
    if event_type == "message":
        return _message_event(record)
    if event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary", "")
        if not isinstance(summary, str):
            raise ValueError("session summaries must be strings")  # noqa: TRY003
        return (event_type, f"{event_type}: {summary}") if summary else None
    if event_type == "custom_message":
        body = _text_content(record.get("content"))
        return ("custom", f"custom: {body}") if body else None
    return None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")  # noqa: TRY003


def _jsonl_records(content: bytes) -> list[dict[str, JsonValue]]:
    records: list[dict[str, JsonValue]] = []
    try:
        # JSONL separates records by LF; Unicode line separators inside strings
        # belong to the original text and must not split a record.
        for line in content.decode("utf-8-sig", errors="strict").split("\n"):
            if not line.strip():
                continue
            record: object = json.loads(line, parse_constant=_reject_json_constant)
            if not isinstance(record, dict):
                raise ValueError("each Pi JSONL record must be an object")  # noqa: TRY003, TRY004
            records.append(cast(dict[str, JsonValue], record))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid UTF-8 Pi JSONL: {error}") from error  # noqa: TRY003
    if not records:
        raise ValueError("Pi sessions must start with a session header")  # noqa: TRY003
    return records


def _node_identity(record: dict[str, JsonValue], known: dict[str, dict[str, JsonValue]]) -> tuple[str, str | None]:
    event_id = _nonblank_string(record.get("id"), "entry.id")
    if _nonblank_string(record.get("type"), "entry.type") == "session":
        raise ValueError("a session header must only appear at the start")  # noqa: TRY003
    if event_id in known:
        raise ValueError(f"duplicate Pi entry ID: {event_id}")  # noqa: TRY003
    if "parentId" not in record:
        raise ValueError("Pi entries must contain parentId")  # noqa: TRY003
    parent = record["parentId"]
    if parent is not None:
        parent = _nonblank_string(parent, "entry.parentId")
        if parent not in known:
            raise ValueError(f"Pi parent must refer to an earlier entry: {parent}")  # noqa: TRY003
    return event_id, parent


@dataclass(frozen=True)
class _ParsedSession:
    session_id: str
    events: tuple[PiEventInput, ...]
    fingerprints: dict[str, str]


def _parse_session(value: PiSessionInput) -> _ParsedSession:
    records = _jsonl_records(value.content)
    header = records[0]
    if header.get("type") != "session" or type(header.get("version")) is not int or header["version"] != 3:
        raise ValueError("Pi sessions require a version 3 session header")  # noqa: TRY003
    session_id = _nonblank_string(header.get("id"), "session.id")
    _json_text(header).encode("utf-8")
    known: dict[str, dict[str, JsonValue]] = {}
    visible: dict[str, PiEventInput] = {}
    fingerprints: dict[str, str] = {}
    last_id: str | None = None
    for record in records[1:]:
        event_id, parent = _node_identity(record, known)
        fingerprints[event_id] = hashlib.sha256(_json_text(record).encode("utf-8")).hexdigest()
        known[event_id] = record
        text = _visible_event(record)
        if text is not None:
            role, content = text
            visible[event_id] = PiEventInput(
                session_id=session_id,
                event_id=event_id,
                parent_id=parent,
                event_type=cast(str, record["type"]),
                role=role,
                content=content,
                record=record,
            )
        last_id = event_id
    leaf = value.leaf_id if value.leaf_id is not None else last_id
    if leaf is not None and leaf not in known:
        raise ValueError(f"Pi leaf entry does not exist: {leaf}")  # noqa: TRY003
    events: list[PiEventInput] = []
    while leaf is not None:
        if leaf in visible:
            events.append(visible[leaf])
        leaf = cast(str | None, known[leaf]["parentId"])
    return _ParsedSession(session_id, tuple(reversed(events)), fingerprints)


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Validate the whole archive, then return root-to-leaf original evidence."""
    return _parse_session(value).events


class PiEventSource(Source):
    """Captured original event; its full record is retained independently of Memory."""

    model_config = ConfigDict(frozen=True)

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    event: PiEventInput

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.name != _source_id(self.event.session_id, self.event.event_id):
            raise ValueError("Pi source name must match the session/event identity")  # noqa: TRY003
        return self


class _PiEventAdapter:
    input_class = PiEventInput
    name = "pi-event"
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(name=_source_id(value.session_id, value.event_id), event=value.model_copy(deep=True))

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return source.event.model_copy(deep=True)


class _PiEventTextEvidence:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        event = source.event
        return TextEvidence(
            source_type="pi-event",
            source_id=source.name,
            content=event.content,
            metadata={
                "session_id": event.session_id,
                "event_id": event.event_id,
                "parent_id": event.parent_id,
                "event_type": event.event_type,
                "role": event.role,
            },
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    _PiEventAdapter(), version="1", projections=(_PiEventTextEvidence(),)
)


class _Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    session_id: str
    imported: dict[str, str]


def _restore_progress(checkpoint: JsonValue | None, parsed: _ParsedSession) -> dict[str, str]:
    if checkpoint is None:
        return {}
    previous = _Checkpoint.model_validate(checkpoint)
    if previous.session_id != parsed.session_id:
        raise ValueError("the checkpoint belongs to a different Pi session")  # noqa: TRY003
    for event_id, fingerprint in previous.imported.items():
        if parsed.fingerprints.get(event_id) != fingerprint:
            raise ValueError(f"previously imported Pi event was changed or removed: {event_id}")  # noqa: TRY003
    return dict(previous.imported)


class PiSessionConnector:
    """Import newly selected evidence, leaving checkpoint commits to the lifecycle."""

    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({"pi-event"})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        content = await asyncio.to_thread(self.path.read_bytes)
        parsed = _parse_session(PiSessionInput(content=content, leaf_id=self.leaf_id))
        imported = _restore_progress(session.checkpoint, parsed)
        for event in parsed.events:
            if event.event_id in imported:
                continue
            try:
                outcome = await session.submit(_source_id(event.session_id, event.event_id), "pi-event", event)
            except SourceConflictError as error:
                # A partial prior run can have durable Sources but no new checkpoint.
                raise ValueError(f"previously captured Pi event conflicts: {event.event_id}") from error  # noqa: TRY003
            if outcome.status is not ConnectorSubmissionStatus.ACCEPTED:
                return ConnectorRunCompletion(status=ConnectorRunStatus.INCOMPLETE, checkpoint=session.checkpoint)
            imported[event.event_id] = parsed.fingerprints[event.event_id]
        checkpoint = _Checkpoint(session_id=parsed.session_id, imported=imported).model_dump(mode="json")
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=checkpoint)
