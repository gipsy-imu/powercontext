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

"""Capture the visible events of one Pi session branch as immutable Sources.

A Pi session file is a single JSONL stream whose records form a tree through
``parentId`` links. One leaf plus its ancestors selects a history path. This
module turns every *visible* node on that path into one CAPTURED Source, keeps
the node's original record verbatim, and captures incrementally through the
existing Connector lifecycle.

Two rules drive the whole design:

* A control node (``model_change``, ``thinking_level_change``, an assistant
  message that only carries thinking) produces no text evidence, but it must
  never break the ancestry: a child's ``parent_id`` still names it.
* A compaction or branch summary is *not* a replacement for the events before
  it. Those events stay on the path and stay captured, because this history is
  evidence for auditing rather than context for a model to continue a dialogue.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"

#: The only Pi session generation this challenge supports.
_PI_SESSION_VERSION = 3
#: Checkpoint envelope version, so a future format can be recognised and ignored.
_CHECKPOINT_VERSION = 1
#: Records whose ``type`` is not listed here are control nodes.
_MESSAGE_TYPE = "message"
_CUSTOM_MESSAGE_TYPE = "custom_message"
_SUMMARY_TYPES = frozenset({"compaction", "branch_summary"})
#: Separators plus the ``pi`` prefix of the readable identity form.
_IDENTITY_PREFIX = "pi:"


class PiSessionInput(BaseModel):
    """One complete Pi session JSONL document plus the branch point to walk."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch, with its original record.

    ``record`` is the object exactly as it appeared in the JSONL stream. Keeping
    it (instead of a line number or an offset) is what lets ``read(source)``
    return an artifact that is byte-faithful to the input after a restart.
    """

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


class PiEventSource(Source):
    """A captured Source holding exactly one visible Pi session event."""

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Return the stable Source identity for one event.

    The identity depends on the session id and the event id only, so re-importing
    the same event after a restart, a branch switch or a file append resolves to
    the same Source instead of creating a duplicate.

    Both parts are percent-encoded with ``safe=""``: without that, a session id
    containing ``:`` could be confused with the separator and two different
    (session, event) pairs could collide on one identity.
    """

    _require_text(session_id, "session_id")
    _require_text(event_id, "event_id")

    identity = f"{_IDENTITY_PREFIX}{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(identity) <= MAX_SOURCE_ID_LENGTH:
        return identity

    # Long ids are hashed instead of truncated. Truncation would make two events
    # share a prefix and therefore a Source; the distinct ``pi-h:`` prefix keeps a
    # hashed identity from ever colliding with a readable one.
    digest = hashlib.sha256(f"{len(session_id)}:{session_id}:{len(event_id)}:{event_id}".encode()).hexdigest()
    return f"pi-h:{digest[:48]}"


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return every visible event on the selected branch, ordered root to leaf.

    The whole document is parsed and validated before anything is returned, so an
    invalid graph never yields a partial branch. Every failure is reported as
    ``ValueError``, including invalid UTF-8 and malformed JSON, because callers
    treat this function as an all-or-nothing boundary.
    """

    records = _decode_records(value.content)
    header, body = records[0], records[1:]

    if header.get("type") != "session":
        raise ValueError("Pi session must start with a type=session header record")
    if header.get("version") != _PI_SESSION_VERSION:
        raise ValueError(f"Pi session header version must be {_PI_SESSION_VERSION}")
    session_id = header.get("id")
    _require_text(session_id, "Pi session header id")
    assert isinstance(session_id, str)  # noqa: S101 - narrowed by _require_text

    parents, order, by_id = _build_graph(body)
    chain = _resolve_chain(order, parents, value.leaf_id)

    events: list[PiEventInput] = []
    for event_id in chain:
        record = by_id[event_id]
        event = _build_event(session_id, event_id, parents[event_id], record)
        if event is not None:
            events.append(event)
    return tuple(events)


class PiEventSourceAdapter:
    """Resolve and read per-event Pi Sources."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            name=pi_event_source_id(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one event as canonical text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.name,
            # ``content`` already carries the ``ROLE: BODY`` shape, so a consumer
            # searching for a role always hits the event that owns it.
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PiEventSourceAdapter(),
    projections=(PiEventTextEvidenceProjection(),),
)


class PiSessionConnector:
    """Import new events of one Pi branch, resuming from a durable checkpoint.

    Every run re-reads and fully re-validates the file. That is deliberate: the
    imported set lives in the checkpoint, so a damaged input must fail before any
    submission, and an already-imported event that was rewritten in place must be
    detected instead of silently overwriting its evidence.
    """

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        raw = self._path.read_bytes()
        # Any invalid graph or archive raises ValueError here, which leaves the
        # previously committed checkpoint untouched.
        events = parse_pi_session(PiSessionInput(content=raw, leaf_id=self._leaf_id))

        imported = _read_checkpoint(session.checkpoint)
        merged = dict(imported)
        for event in events:
            identity = pi_event_source_id(event.session_id, event.event_id)
            digest = _record_digest(event.record)
            known = imported.get(identity)
            if known is not None:
                if known != digest:
                    raise ValueError(
                        f"already imported Pi event {event.event_id!r} was rewritten; "
                        "refusing to overwrite its captured evidence"
                    )
                continue
            await session.submit(identity, PI_EVENT_SOURCE_NAME, event)
            merged[identity] = digest

        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"version": _CHECKPOINT_VERSION, "events": merged},
        )


def _decode_records(content: object) -> list[dict[str, Any]]:
    """Decode UTF-8 JSONL into records, rejecting anything unusable."""

    if not isinstance(content, (bytes, bytearray)):
        raise ValueError("Pi session content must be bytes")
    try:
        # utf-8-sig removes a leading BOM when present and is a no-op otherwise.
        text = bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be valid UTF-8") from error

    records: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Pi session line {number} is not valid JSON: {error}") from error
        if not isinstance(record, dict):
            raise ValueError(f"Pi session line {number} must be a JSON object")
        records.append(record)

    if not records:
        raise ValueError("Pi session must contain a header record")
    return records


def _build_graph(
    body: list[dict[str, Any]],
) -> tuple[dict[str, str | None], list[str], dict[str, dict[str, Any]]]:
    """Validate the parent links and return ``parents``, insertion order and records.

    A parent must already have appeared, so the stream is a forest by
    construction: a forward reference is rejected rather than resolved, which
    rules out cycles before any traversal happens.
    """

    parents: dict[str, str | None] = {}
    order: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}

    for number, record in enumerate(body, start=2):
        event_id = record.get("id")
        _require_text(event_id, f"Pi session record on line {number} id")
        assert isinstance(event_id, str)  # noqa: S101 - narrowed by _require_text

        if event_id in parents:
            raise ValueError(f"duplicate Pi session record id: {event_id!r}")

        parent_id = record.get("parentId")
        if parent_id is not None and not isinstance(parent_id, str):
            raise ValueError(f"Pi session record {event_id!r} parentId must be a string or null")
        # ``parent_id == event_id`` fails here too, because the id is registered
        # only after its parent has been accepted.
        if parent_id is not None and parent_id not in parents:
            raise ValueError(f"Pi session record {event_id!r} references unknown parent {parent_id!r}")

        parents[event_id] = parent_id
        order.append(event_id)
        by_id[event_id] = record

    return parents, order, by_id


def _resolve_chain(order: list[str], parents: dict[str, str | None], leaf_id: str | None) -> list[str]:
    """Return the root-to-leaf ancestry of the selected node."""

    if leaf_id is None:
        if not order:
            return []
        leaf = order[-1]
    else:
        _require_text(leaf_id, "leaf_id")
        if leaf_id not in parents:
            raise ValueError(f"Pi session leaf {leaf_id!r} does not exist")
        leaf = leaf_id

    chain: list[str] = []
    cursor: str | None = leaf
    while cursor is not None:
        chain.append(cursor)
        cursor = parents[cursor]
    chain.reverse()
    return chain


def _build_event(
    session_id: str,
    event_id: str,
    parent_id: str | None,
    record: dict[str, Any],
) -> PiEventInput | None:
    """Build one event, or return ``None`` for a node with no visible text."""

    record_type = record.get("type")
    if record_type == _MESSAGE_TYPE:
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        body = _message_body(role, message)
    elif isinstance(record_type, str) and record_type in _SUMMARY_TYPES:
        role = record_type
        summary = record.get("summary")
        body = summary if isinstance(summary, str) else ""
    elif record_type == _CUSTOM_MESSAGE_TYPE:
        role = "custom"
        body = "\n".join(_text_parts(record.get("content")))
    else:
        # Control node: keeps its place in the graph but contributes no evidence.
        return None

    # "Only ignore events without visible body text" - a blank body is the sole
    # reason to drop a node that has a known shape.
    if not body.strip():
        return None

    return PiEventInput(
        session_id=session_id,
        event_id=event_id,
        parent_id=parent_id,
        event_type=record_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _message_body(role: str, message: dict[str, Any]) -> str:
    """Render one message's body according to its role."""

    if role == "toolResult":
        tool_name = _scalar_text(message.get("toolName")) or "unknown"
        tool_call_id = _scalar_text(message.get("toolCallId")) or "unknown"
        text = "\n".join(_text_parts(message.get("content")))
        return f"[tool_result {tool_name} {tool_call_id}] {text}"

    if role == "bashExecution":
        command = _scalar_text(message.get("command"))
        output = _scalar_text(message.get("output"))
        if not command and not output:
            return ""
        return f"$ {command}\n{output}"

    content = message.get("content")
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content if isinstance(content, list) else ():
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block_type == "toolCall":
            name = _scalar_text(block.get("name")) or "unknown"
            call_id = _scalar_text(block.get("id")) or "unknown"
            arguments = block.get("arguments")
            parts.append(f"[tool_call {name} {call_id}] {_compact_json(arguments)}")
        # thinking and image blocks never become text evidence.
    # Blocks are joined by newlines so one multi-block turn stays one event.
    return "\n".join(parts)


def _text_parts(content: object) -> list[str]:
    """Return the visible strings of a Pi content value."""

    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]


def _compact_json(value: object) -> str:
    """Serialise tool arguments with sorted keys and no extra whitespace."""

    if value is None:
        value = {}
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _scalar_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _read_checkpoint(checkpoint: object) -> dict[str, str]:
    """Read the imported-event map, tolerating a foreign or corrupt checkpoint.

    Returning an empty map is safe: re-submitting an identical observation is
    idempotent, so the worst case is a redundant replay rather than lost data.
    """

    if not isinstance(checkpoint, dict):
        return {}
    if checkpoint.get("version") != _CHECKPOINT_VERSION:
        return {}
    events = checkpoint.get("events")
    if not isinstance(events, dict):
        return {}
    return {key: value for key, value in events.items() if isinstance(key, str) and isinstance(value, str)}


def _record_digest(record: dict[str, Any]) -> str:
    """Fingerprint one original record so an in-place rewrite can be detected."""

    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_CONNECTOR_VERSION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
