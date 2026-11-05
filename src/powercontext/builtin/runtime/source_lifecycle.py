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

"""Persist explicit evidence dependencies and reconcile native Memory state."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory.service import MemoryService
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.source_lifecycle import SourceLifecycleRepository, StoredLifecycleNode
from powercontext.builtin.records import BaseAccessError
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.errors import PowerContextError
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from powercontext.builtin.runtime.application import BuiltinRuntime


class SourceLifecycleError(ValueError):
    """Invalid lifecycle identity, dependency, event or generation use."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"source lifecycle: {reason}")


class SourceLifecycleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    node_id: str
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        return _nonblank(value)


class _Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    node_id: str
    kind: Literal["source", "memory", "topic"]
    reference: SourceRef | MemoryCitation | ArtifactRef
    parents: tuple[str, ...] = ()
    policy: Literal["all", "any"] = "all"
    revision: int | None = None


@dataclass(slots=True)
class _Node:
    binding: _Binding
    available: bool
    last_event: SourceLifecycleEvent | None = None


class ScopedSourceLifecycle:
    """One single-writer Scope's immutable DAG and recoverable propagation."""

    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase, scope_id: str) -> None:
        self._runtime = runtime
        self._database = database
        self.scope_id = validate_scope_id(scope_id)
        self._repository = SourceLifecycleRepository()

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("invalid-revision")
        await self._register(
            _Binding(node_id=_nonblank(node_id), kind="source", reference=source_ref, revision=revision)
        )

    async def register_memory(
        self, node_id: str, citation: MemoryCitation, *, parents: tuple[str, ...], policy: Literal["all", "any"]
    ) -> None:
        await self._register(_derived_binding(node_id, "memory", citation, parents, policy))

    async def register_topic(
        self, node_id: str, artifact_ref: ArtifactRef, *, parents: tuple[str, ...], policy: Literal["all", "any"]
    ) -> None:
        await self._register(_derived_binding(node_id, "topic", artifact_ref, parents, policy))

    async def _register(self, binding: _Binding) -> None:
        binding = binding.model_copy(deep=True)
        async with self._runtime._context(self.scope_id) as context, self._runtime._locked(self.scope_id):
            await self._read_reference(binding)
            async with self._database.transaction() as connection:
                nodes = await self._load(connection)
                existing = nodes.get(binding.node_id)
                if existing is not None:
                    if existing.binding != binding:
                        raise SourceLifecycleError("conflicting-binding")
                else:
                    if any(parent not in nodes for parent in binding.parents):
                        raise SourceLifecycleError("missing-parent")
                    available = True if binding.kind == "source" else _supported(binding, nodes)
                    node = _Node(binding, available)
                    nodes[binding.node_id] = node
                    ref = binding.reference
                    await self._repository.add(
                        connection,
                        self.scope_id,
                        StoredLifecycleNode(
                            node_id=binding.node_id,
                            kind=binding.kind,
                            binding=binding.model_dump_json().encode(),
                            available=available,
                            artifact_id=ref.artifact_id if isinstance(ref, ArtifactRef) else None,
                            artifact_revision=ref.revision if isinstance(ref, ArtifactRef) else None,
                        ),
                    )
            await _sync_memory(
                context.artifacts.memory, context.artifacts.memory_artifact_id, nodes, "lifecycle registration"
            )

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("invalid-event")
        async with self._runtime._context(self.scope_id) as context, self._runtime._locked(self.scope_id):
            async with self._database.transaction() as connection:
                nodes = await self._load(connection)
                root = nodes.get(event.node_id)
                if root is None or root.binding.kind != "source":
                    raise SourceLifecycleError("unknown-source")
                if root.binding.revision != event.revision:
                    raise SourceLifecycleError("revision-mismatch")
                previous = root.last_event
                if previous is not None and (
                    event.sequence < previous.sequence or (event.sequence == previous.sequence and event != previous)
                ):
                    raise SourceLifecycleError("event-sequence-conflict")
                root.available = event.available
                desired = _availability(nodes)
                for node_id, available in desired.items():
                    if nodes[node_id].available != available or node_id == event.node_id:
                        await self._repository.set_state(connection, self.scope_id, node_id, available)
                    nodes[node_id].available = available
                if previous != event:
                    await self._repository.set_event(
                        connection, self.scope_id, event.node_id, event.model_dump_json().encode()
                    )
            # Desired state and the exact event commit before native mutation.
            # Retry the same event by reconciling actual manifest states, not by
            # assuming that an accepted event already finished propagation.
            await _sync_memory(context.artifacts.memory, context.artifacts.memory_artifact_id, nodes, event.reason)
            return sorted(node_id for node_id, node in nodes.items() if node.available)

    async def resolve(
        self, node_id: str, *, purpose: Literal["generation", "audit"]
    ) -> SourceRef | MemoryCitation | ArtifactRef:
        if purpose not in ("generation", "audit"):
            raise SourceLifecycleError("unknown-purpose")
        async with self._runtime._context(self.scope_id), self._database.transaction() as connection:
            node = (await self._load(connection)).get(_nonblank(node_id))
        if node is None:
            raise SourceLifecycleError("unknown-node")
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError("unavailable-node")
        return node.binding.reference.model_copy(deep=True)

    async def _load(self, connection: AsyncConnection) -> dict[str, _Node]:
        return {
            stored.node_id: _Node(
                binding=_Binding.model_validate_json(stored.binding),
                available=stored.available,
                last_event=None
                if stored.last_event is None
                else SourceLifecycleEvent.model_validate_json(stored.last_event),
            )
            for stored in await self._repository.load(connection, self.scope_id)
        }

    async def _read_reference(self, binding: _Binding) -> None:
        ref = binding.reference
        try:
            if binding.kind == "source" and isinstance(ref, SourceRef):
                await self._runtime.records.for_scope(self.scope_id).get_source(ref.source_type, ref.source_id)
            elif binding.kind == "memory" and isinstance(ref, MemoryCitation) and ref.memory_ref.family == "memory":
                await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=ref))
            elif binding.kind == "topic" and isinstance(ref, ArtifactRef) and ref.family == "topic-memory":
                await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                    ref.family, ref.artifact_id, ref.revision
                )
            else:
                raise SourceLifecycleError("reference-kind-mismatch")
        except (PowerContextError, BaseAccessError) as error:
            raise SourceLifecycleError("unreadable-reference") from error


class SourceLifecycleApplication:
    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase | None) -> None:
        self._runtime = runtime
        self._database = database

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycle:
        if self._database is None:
            raise SourceLifecycleError("lifecycle-not-configured")
        return ScopedSourceLifecycle(self._runtime, self._database, scope_id)


def _nonblank(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceLifecycleError("blank-identifier-or-reason")
    return value


def _derived_binding(
    node_id: str,
    kind: Literal["memory", "topic"],
    reference: MemoryCitation | ArtifactRef,
    parents: tuple[str, ...],
    policy: Literal["all", "any"],
) -> _Binding:
    node_id = _nonblank(node_id)
    if not isinstance(parents, (tuple, list)) or not parents or policy not in ("all", "any"):
        raise SourceLifecycleError("invalid-dependencies")
    checked = tuple(_nonblank(parent) for parent in parents)
    if node_id in checked or len(set(checked)) != len(checked):
        raise SourceLifecycleError("cyclic-or-duplicate-parent")
    return _Binding(node_id=node_id, kind=kind, reference=reference, parents=tuple(sorted(checked)), policy=policy)


def _supported(binding: _Binding, nodes: dict[str, _Node]) -> bool:
    support = tuple(nodes[parent].available for parent in binding.parents)
    return all(support) if binding.policy == "all" else any(support)


def _availability(nodes: dict[str, _Node]) -> dict[str, bool]:
    result: dict[str, bool] = {}
    visiting: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in result:
            return result[node_id]
        if node_id in visiting or node_id not in nodes:
            raise SourceLifecycleError("invalid-dependency-graph")
        visiting.add(node_id)
        node = nodes[node_id]
        support = tuple(visit(parent) for parent in node.binding.parents)
        result[node_id] = (
            node.available
            if node.binding.kind == "source"
            else (all(support) if node.binding.policy == "all" else any(support))
        )
        visiting.remove(node_id)
        return result[node_id]

    for node_id in nodes:
        visit(node_id)
    return result


async def _sync_memory(service: MemoryService, artifact_id: str, nodes: dict[str, _Node], reason: str) -> None:
    # The full event reason remains durable and participates in replay identity.
    # Native Memory change reasons have a separate 512-character audit limit.
    audit_reason = unicodedata.normalize("NFC", reason).strip()[:512]
    wanted: dict[tuple[str, str], bool] = {}
    for node in nodes.values():
        ref = node.binding.reference
        if node.binding.kind == "memory" and isinstance(ref, MemoryCitation):
            key = (ref.entry_id, ref.entry_version_id)
            wanted[key] = wanted.get(key, False) or node.available
    if not wanted:
        return
    current = await service.head(artifact_id)
    entries = await service.entries(current)
    current_keys = {(entry.entry_id, entry.entry_version_id) for entry in entries}
    if wanted.keys() - current_keys:
        raise SourceLifecycleError("bound-memory-body-changed")
    states = {item.entry_id: item.state for item in current.content.manifest.entries}
    for target in (False, True):
        selected = tuple(
            entry
            for entry in entries
            if wanted.get((entry.entry_id, entry.entry_version_id)) is target
            and (states[entry.entry_id] == "active") != target
        )
        if selected:
            operation = service.reactivate if target else service.forget
            current = await operation(current, entries=selected, reason=audit_reason)


__all__ = ["SourceLifecycleError", "SourceLifecycleEvent"]
