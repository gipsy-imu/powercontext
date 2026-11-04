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

"""Validated UTF-8 context packs and independently reconstructible prefix deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from typing import cast

__all__ = ["ContextPack", "ContextPackDelta", "ContextPackError", "apply_context_pack_delta"]

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"


class ContextPackError(ValueError):
    """A context pack violates its encoding, integrity, or chunk contract."""


def _json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def _validate_items(items: tuple[bytes, ...]) -> None:
    if not isinstance(items, tuple) or any(not isinstance(item, bytes) for item in items):
        raise ContextPackError("chunks must be a tuple of bytes")  # noqa: TRY003
    try:
        for item in items:
            item.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ContextPackError("every chunk must contain valid UTF-8") from error  # noqa: TRY003


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("wire chunks must be a Base64 string array")  # noqa: TRY003
    items = []
    for encoded in value:
        if not isinstance(encoded, str):
            raise ContextPackError("wire chunks must be Base64 strings")  # noqa: TRY003
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ContextPackError("invalid Base64 chunk") from error  # noqa: TRY003
        if base64.b64encode(decoded).decode("ascii") != encoded:
            raise ContextPackError("chunks must use canonical standard Base64")  # noqa: TRY003
        items.append(decoded)
    result = tuple(items)
    _validate_items(result)
    return result


def _validate_digest(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ContextPackError("digest must be sha256 followed by 64 lowercase hexadecimal characters")  # noqa: TRY003


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContextPackError(f"duplicate wire field: {key}")  # noqa: TRY003
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ContextPackError(f"invalid JSON constant: {value}")  # noqa: TRY003


def _decode_wire(data: bytes, schema: str, fields: set[str]) -> dict[str, object]:
    if not isinstance(data, bytes):
        raise ContextPackError("wire input must be UTF-8 bytes")  # noqa: TRY003
    try:
        value: object = json.loads(
            data.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ContextPackError(f"invalid context pack JSON: {error}") from error  # noqa: TRY003
    if not isinstance(value, dict) or set(value) != fields or value.get("schema") != schema:
        raise ContextPackError("wire schema or fields do not match the context pack protocol")  # noqa: TRY003
    return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class ContextPack:
    """An immutable complete pack with self-consistent canonical wire metadata."""

    items: tuple[bytes, ...]
    digest: str = field(init=False)
    byte_count: int = field(init=False)

    def __post_init__(self) -> None:
        _validate_items(self.items)
        unsigned: dict[str, object] = {"schema": _PACK_SCHEMA, "items": _encode_items(self.items)}
        digest = "sha256:" + hashlib.sha256(_json_bytes(unsigned)).hexdigest()
        size = 0
        while True:
            measured = len(_json_bytes({**unsigned, "digest": digest, "byte_count": size}))
            if measured == size:
                break
            size = measured
        object.__setattr__(self, "digest", digest)
        object.__setattr__(self, "byte_count", size)

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        return cls(items)

    @classmethod
    def from_content(cls, content: str | bytes | None, *, chunk_bytes: int = 512) -> ContextPack:
        """Split deterministically at line and UTF-8 boundaries, retaining all bytes."""
        if type(chunk_bytes) is not int or chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
        try:
            if content is None:
                raw = b""
            elif isinstance(content, str):
                raw = content.encode("utf-8", errors="strict")
            elif isinstance(content, bytes):
                content.decode("utf-8", errors="strict")
                raw = content
            else:
                raise ContextPackError("content must be text, UTF-8 bytes, or None")  # noqa: TRY003
        except UnicodeError as error:
            raise ContextPackError("content must be valid UTF-8") from error  # noqa: TRY003
        items = []
        for line in raw.splitlines(keepends=True):
            start = 0
            while start < len(line):
                end = min(start + chunk_bytes, len(line))
                while end < len(line) and line[end] & 0xC0 == 0x80:
                    end -= 1
                if end == start:
                    raise ContextPackError("chunk_bytes cannot contain the next UTF-8 character")  # noqa: TRY003
                items.append(line[start:end])
                start = end
        return cls.build(tuple(items))

    def to_bytes(self) -> bytes:
        return _json_bytes({
            "schema": _PACK_SCHEMA,
            "items": _encode_items(self.items),
            "digest": self.digest,
            "byte_count": self.byte_count,
        })

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        payload = _decode_wire(data, _PACK_SCHEMA, {"schema", "items", "digest", "byte_count"})
        result = cls.build(_decode_items(payload["items"]))
        if payload["digest"] != result.digest:
            raise ContextPackError("pack digest does not match its chunks")  # noqa: TRY003
        if type(payload["byte_count"]) is not int or payload["byte_count"] != result.byte_count:
            raise ContextPackError("pack byte_count does not match its canonical wire size")  # noqa: TRY003
        return result


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """Keep a validated base prefix and append the complete target suffix."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _validate_digest(self.base_digest)
        _validate_digest(self.target_digest)
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("prefix_items must be a nonnegative integer")  # noqa: TRY003
        _validate_items(self.appended)

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("base and target must be complete ContextPack values")  # noqa: TRY003
        prefix = 0
        for previous, current in zip(base.items, target.items, strict=False):
            if previous != current:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    def to_bytes(self) -> bytes:
        return _json_bytes({
            "schema": _DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(self.appended),
        })

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        payload = _decode_wire(
            data, _DELTA_SCHEMA, {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
        )
        _validate_digest(payload["base_digest"])
        _validate_digest(payload["target_digest"])
        prefix = payload["prefix_items"]
        if type(prefix) is not int or prefix < 0:
            raise ContextPackError("prefix_items must be a nonnegative integer")  # noqa: TRY003
        return cls(
            cast(str, payload["base_digest"]),
            cast(str, payload["target_digest"]),
            prefix,
            _decode_items(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("applying a delta requires a complete base pack and a ContextPackDelta")  # noqa: TRY003
    if base.digest != delta.base_digest:
        raise ContextPackError("delta base digest does not match the supplied pack")  # noqa: TRY003
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds the base chunk count")  # noqa: TRY003
    target = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("delta target digest does not match the reconstructed pack")  # noqa: TRY003
    return target
