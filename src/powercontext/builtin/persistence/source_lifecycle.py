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

"""Persist lifecycle bindings and qualify current Topic retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Integer, cast, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import ColumnElement

from powercontext.builtin.persistence.codec import stored_bytes
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE


@dataclass(frozen=True, slots=True)
class StoredLifecycleNode:
    node_id: str
    kind: str
    binding: bytes
    available: bool
    last_event: bytes | None = None
    artifact_id: str | None = None
    artifact_revision: int | None = None


class SourceLifecycleRepository:
    async def load(self, connection: AsyncConnection, scope_id: str) -> tuple[StoredLifecycleNode, ...]:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        rows = (await connection.execute(select(table).where(table.c.scope_id == scope_id))).mappings()
        return tuple(
            StoredLifecycleNode(
                node_id=str(row["node_id"]),
                kind=str(row["kind"]),
                binding=stored_bytes(row["binding"], column="binding"),
                available=bool(row["available"]),
                last_event=None if row["last_event"] is None else stored_bytes(row["last_event"], column="last_event"),
                artifact_id=row["artifact_id"],
                artifact_revision=row["artifact_revision"],
            )
            for row in rows
        )

    async def add(self, connection: AsyncConnection, scope_id: str, node: StoredLifecycleNode) -> None:
        await connection.execute(
            insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                scope_id=scope_id,
                node_id=node.node_id,
                kind=node.kind,
                binding=node.binding,
                available=node.available,
                artifact_id=node.artifact_id,
                artifact_revision=node.artifact_revision,
            )
        )

    async def set_state(self, connection: AsyncConnection, scope_id: str, node_id: str, available: bool) -> None:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        await connection.execute(
            update(table).where(table.c.scope_id == scope_id, table.c.node_id == node_id).values(available=available)
        )

    async def set_event(self, connection: AsyncConnection, scope_id: str, node_id: str, payload: bytes) -> None:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        await connection.execute(
            update(table).where(table.c.scope_id == scope_id, table.c.node_id == node_id).values(last_event=payload)
        )


def topic_lifecycle_available(
    scope_id: str, artifact_id: ColumnElement[Any], revision: ColumnElement[Any]
) -> ColumnElement[bool]:
    """Unbound Topics and Topics with any available binding remain eligible."""

    nodes = SOURCE_LIFECYCLE_NODES_TABLE.alias("lifecycle_topic")
    bound = (
        select(nodes.c.node_id)
        .where(
            nodes.c.scope_id == scope_id,
            nodes.c.kind == "topic",
            nodes.c.artifact_id == artifact_id,
            nodes.c.artifact_revision == revision,
        )
        .correlate_except(nodes)
    )
    return or_(~bound.exists(), bound.where(nodes.c.available.is_(True)).exists())


async def unavailable_topic_refs(connection: AsyncConnection, scope_id: str) -> frozenset[tuple[str, int]]:
    nodes = SOURCE_LIFECYCLE_NODES_TABLE
    rows = await connection.execute(
        select(nodes.c.artifact_id, nodes.c.artifact_revision)
        .where(nodes.c.scope_id == scope_id, nodes.c.kind == "topic")
        .group_by(nodes.c.artifact_id, nodes.c.artifact_revision)
        .having(func.max(cast(nodes.c.available, Integer)) == 0)
    )
    return frozenset((str(artifact_id), int(revision)) for artifact_id, revision in rows)
