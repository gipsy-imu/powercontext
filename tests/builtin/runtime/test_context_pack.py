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

import base64
import hashlib
import json
import random

import pytest

from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)


@pytest.mark.parametrize("count", [0, 1, 9, 70, 700])
def test_pack_digest_and_self_inclusive_size_match_independent_wire_oracle(count: int) -> None:
    items = tuple(f"row {number} 北京\r\n".encode() for number in range(count))
    pack = ContextPack.build(items)
    payload = json.loads(pack.to_bytes())
    unsigned = {"schema": "powercontext.context-pack.v1", "items": [base64.b64encode(item).decode() for item in items]}
    digest = (
        "sha256:" + hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    assert payload == {**unsigned, "digest": digest, "byte_count": len(pack.to_bytes())}
    assert pack.digest == digest and pack.byte_count == len(pack.to_bytes())
    assert ContextPack.from_bytes(pack.to_bytes()) == pack


def test_chunking_is_deterministic_bounded_utf8_and_lossless_for_mixed_unicode() -> None:
    generator = random.Random(2718)  # noqa: S311 - Reproducible text fixtures, not cryptographic randomness.
    alphabet = [*"abc \r\n\t海洋é🚀", "\u0085", "\u2028", "\u2029", "\x00", "\u0301"]
    for _ in range(80):
        content = "".join(generator.choices(alphabet, k=100))
        limit = generator.randrange(1, 25)
        if any(len(character.encode()) > limit for character in content):
            with pytest.raises(ContextPackError):
                ContextPack.from_content(content, chunk_bytes=limit)
            continue
        pack = ContextPack.from_content(content, chunk_bytes=limit)
        assert pack == ContextPack.from_content(content, chunk_bytes=limit)
        assert b"".join(pack.items) == content.encode()
        assert all(len(item) <= limit and item.decode().encode() == item for item in pack.items)


@pytest.mark.parametrize("limit", [0, -1, True, False, 3.5, "4", None])
def test_invalid_chunk_limits_raise_the_protocol_error(limit: object) -> None:
    with pytest.raises(ContextPackError):
        ContextPack.from_content("text", chunk_bytes=limit)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("content", ["", b"", None])
def test_empty_content_has_no_chunks(content: str | bytes | None) -> None:
    pack = ContextPack.from_content(content)
    assert pack.items == () and ContextPack.from_bytes(pack.to_bytes()) == pack


@pytest.mark.parametrize("items", [(b"\xff",), (b"\xf0\x9f",), ("text",), [b"text"]])
def test_build_rejects_invalid_chunks(items: object) -> None:
    with pytest.raises(ContextPackError):
        ContextPack.build(items)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema", "unknown"),
        ("digest", "sha256:" + "0" * 64),
        ("byte_count", True),
        ("byte_count", 1.0),
        ("byte_count", -1),
        ("byte_count", 0),
        ("items", "YQ=="),
        ("items", [7]),
        ("items", [" YQ=="]),
        ("items", ["YQ"]),
        ("items", ["YR=="]),
        ("items", ["_w=="]),
        ("items", ["/w=="]),
    ],
)
def test_pack_wire_rejects_metadata_and_encoding_tampering(key: str, value: object) -> None:
    payload = json.loads(ContextPack.build((b"a",)).to_bytes())
    payload[key] = value
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps(payload).encode())


@pytest.mark.parametrize(
    "wire",
    [
        b"",
        b"null",
        b"[]",
        b"{",
        b"\xff",
        b'{"schema":NaN}',
        b'{"schema":"powercontext.context-pack.v1","schema":"powercontext.context-pack.v1"}',
    ],
)
def test_non_object_or_malformed_wire_is_rejected(wire: bytes) -> None:
    for decoder in (ContextPack.from_bytes, ContextPackDelta.from_bytes):
        with pytest.raises(ContextPackError):
            decoder(wire)


def test_all_missing_and_extra_wire_fields_fail() -> None:
    base = ContextPack.build((b"one",))
    delta = ContextPackDelta.between(base, ContextPack.build((b"one", b"two")))
    for item, decoder in ((base, ContextPack.from_bytes), (delta, ContextPackDelta.from_bytes)):
        payload = json.loads(item.to_bytes())
        for key in payload:
            with pytest.raises(ContextPackError):
                decoder(json.dumps({name: value for name, value in payload.items() if name != key}).encode())
        with pytest.raises(ContextPackError):
            decoder(json.dumps({**payload, "extra": 1}).encode())


@pytest.mark.parametrize("prefix", [False, True, 1.5, -1, "1", None])
def test_delta_prefix_types_are_not_coerced(prefix: object) -> None:
    base = ContextPack.build((b"a",))
    payload = json.loads(ContextPackDelta.between(base, base).to_bytes())
    payload["prefix_items"] = prefix
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps(payload).encode())


@pytest.mark.parametrize("prefix", [0, 1, 2])
def test_receiver_accepts_every_valid_shared_prefix_including_zero(prefix: int) -> None:
    base = ContextPack.build((b"one\n", "海洋".encode(), b"old"))
    target = ContextPack.build((b"one\n", "海洋".encode(), b"new", b"tail"))
    sent = ContextPackDelta(base.digest, target.digest, prefix, target.items[prefix:])
    received = ContextPackDelta.from_bytes(sent.to_bytes())
    rebuilt = apply_context_pack_delta(ContextPack.from_bytes(base.to_bytes()), received)
    assert rebuilt == target and rebuilt.to_bytes() == target.to_bytes()
    assert received.byte_count == len(sent.to_bytes())


def test_wrong_base_bounds_and_target_are_rejected_on_application() -> None:
    base, target = ContextPack.build((b"original",)), ContextPack.build((b"new",))
    valid = ContextPackDelta.between(base, target)
    invalid = [
        ContextPackDelta("sha256:" + "0" * 64, target.digest, 0, target.items),
        ContextPackDelta(base.digest, target.digest, 2, target.items),
        ContextPackDelta(base.digest, "sha256:" + "0" * 64, 0, target.items),
    ]
    for delta in invalid:
        with pytest.raises(ContextPackError):
            apply_context_pack_delta(base, delta)
    assert apply_context_pack_delta(base, valid) == target
