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

"""Context Pack: an incremental wire format for prepared context bodies.

A :class:`ContextPack` carries the UTF-8 body of one prepared context as an
ordered list of byte chunks. A :class:`ContextPackDelta` carries only what a
receiver needs to rebuild a target pack from a pack it already holds: how many
leading base chunks to keep, and the target's remaining chunks.

Both wire formats are receiver interoperability contracts:

* ``powercontext.context-pack.v1`` carries ``schema``, ``items``, ``digest``
  and ``byte_count``.
* ``powercontext.context-pack-delta.v1`` carries ``schema``, ``base_digest``,
  ``target_digest``, ``prefix_items`` and ``appended``.

Every payload is canonical JSON: UTF-8, sorted keys, ``,``/``:`` separators and
no extra whitespace. A pack digest is ``sha256:<hex>`` over the canonical JSON
of the *unsigned* payload, so a receiver can verify the chunks it decoded
without trusting the transport.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512
_DIGEST_PREFIX = "sha256:"
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_PACK_KEYS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_KEYS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})


class ContextPackError(ValueError):
    """Raised when a pack or delta violates the wire contract."""


def _canonical(payload: dict[str, Any]) -> bytes:
    """Encode one payload as canonical JSON bytes."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(payload: dict[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of one canonical payload."""

    return f"{_DIGEST_PREFIX}{hashlib.sha256(_canonical(payload)).hexdigest()}"


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    """Encode chunks as the standard Base64 strings of the wire format."""

    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(values: Any, *, field: str) -> tuple[bytes, ...]:
    """Decode the Base64 chunk list of a wire payload."""

    if not isinstance(values, list):
        raise ContextPackError(f"pack-invalid-{field}")
    items: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise ContextPackError(f"pack-invalid-{field}")
        try:
            items.append(base64.b64decode(value, validate=True))
        except (binascii.Error, ValueError) as error:
            raise ContextPackError(f"pack-invalid-{field}") from error
    return tuple(items)


def _require_digest(value: Any, *, field: str) -> str:
    """Validate one ``sha256:<hex>`` digest field."""

    if not isinstance(value, str) or not _DIGEST_PATTERN.match(value):
        raise ContextPackError(f"pack-invalid-{field}")
    return value


def _require_prefix(value: Any) -> int:
    """Validate one ``prefix_items`` field."""

    if type(value) is not int or value < 0:
        raise ContextPackError("pack-invalid-prefix_items")
    return value


def _require_keys(payload: Any, keys: frozenset[str], *, kind: str) -> dict[str, Any]:
    """Reject payloads that miss a field or carry an unknown one."""

    if not isinstance(payload, dict):
        raise ContextPackError(f"pack-invalid-{kind}")
    if set(payload) != keys:
        raise ContextPackError(f"pack-invalid-{kind}")
    return payload


def _decode_payload(data: bytes, *, kind: str) -> dict[str, Any]:
    """Parse one canonical JSON object."""

    if isinstance(data, (bytes, bytearray, memoryview)):
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContextPackError(f"pack-invalid-{kind}") from error
    elif isinstance(data, str):
        text = data
    else:
        raise ContextPackError(f"pack-invalid-{kind}")
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError) as error:
        raise ContextPackError(f"pack-invalid-{kind}") from error
    if not isinstance(payload, dict):
        raise ContextPackError(f"pack-invalid-{kind}")
    return payload


def _chunk_bytes(data: bytes, *, chunk_bytes: int) -> tuple[bytes, ...]:
    """Split raw bytes into UTF-8 valid chunks of at most ``chunk_bytes`` bytes."""

    chunks: list[bytes] = []
    current: list[str] = []
    current_bytes = 0
    for character in data.decode("utf-8"):
        encoded = len(character.encode("utf-8"))
        if encoded > chunk_bytes:
            raise ContextPackError("pack-chunk-boundary")
        if current_bytes + encoded > chunk_bytes:
            chunks.append("".join(current).encode("utf-8"))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += encoded
    if current:
        chunks.append("".join(current).encode("utf-8"))
    return tuple(chunks)


@dataclass(frozen=True)
class ContextPack:
    """One complete prepared context body, split into ordered byte chunks."""

    items: tuple[bytes, ...]
    digest: str

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        """Build a complete pack from already chunked bytes."""

        if isinstance(items, (bytes, bytearray, memoryview)) or not isinstance(items, (tuple, list)):
            raise ContextPackError("pack-invalid-items")
        chunks = tuple(bytes(item) for item in items)
        return cls(items=chunks, digest=_digest({"schema": PACK_SCHEMA, "items": _encode_items(chunks)}))

    @classmethod
    def from_content(cls, content: str | None, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Build a pack from one prepared context body.

        The body is split deterministically into chunks of at most
        ``chunk_bytes`` UTF-8 bytes. Every chunk is valid UTF-8 on its own and
        the chunks concatenate back to the exact body bytes.
        """

        if type(chunk_bytes) is not int or chunk_bytes < 1:
            raise ContextPackError("pack-invalid-chunk_bytes")
        if content is None:
            return cls.build(())
        if isinstance(content, (bytes, bytearray, memoryview)):
            body = bytes(content)
        elif isinstance(content, str):
            body = content.encode("utf-8")
        else:
            raise ContextPackError("pack-invalid-content")
        return cls.build(_chunk_bytes(body, chunk_bytes=chunk_bytes))

    @property
    def byte_count(self) -> int:
        """The exact number of bytes this pack occupies on the wire."""

        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Serialize the pack as canonical JSON bytes."""

        payload: dict[str, Any] = {
            "schema": PACK_SCHEMA,
            "items": _encode_items(self.items),
            "digest": self.digest,
        }
        count = len(_canonical({**payload, "byte_count": 0}))
        while True:
            encoded = _canonical({**payload, "byte_count": count})
            if len(encoded) == count:
                return encoded
            count = len(encoded)

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Parse and verify one serialized pack."""

        payload = _require_keys(_decode_payload(data, kind="pack"), _PACK_KEYS, kind="pack")
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("pack-invalid-schema")
        items = _decode_items(payload["items"], field="items")
        digest = _require_digest(payload["digest"], field="digest")
        expected = _digest({"schema": PACK_SCHEMA, "items": _encode_items(items)})
        if digest != expected:
            raise ContextPackError("pack-digest-mismatch")
        byte_count = payload["byte_count"]
        if type(byte_count) is not int or byte_count < 0:
            raise ContextPackError("pack-invalid-byte_count")
        pack = cls(items=items, digest=digest)
        if byte_count != len(pack.to_bytes()):
            raise ContextPackError("pack-byte-count-mismatch")
        return pack


@dataclass(frozen=True)
class ContextPackDelta:
    """The difference between two packs: a kept base prefix and an appended suffix."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Describe how to rebuild ``target`` from ``base``.

        The prefix is the longest run of leading chunks the two packs share.
        Any shared prefix would rebuild the same target, so the longest one is
        used to keep the transfer small.
        """

        shared = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            shared += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=shared,
            appended=target.items[shared:],
        )

    @property
    def byte_count(self) -> int:
        """The exact number of bytes this delta occupies on the wire."""

        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Serialize the delta as canonical JSON bytes."""

        payload: dict[str, Any] = {
            "schema": DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(self.appended),
        }
        return _canonical(payload)

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        """Parse and verify one serialized delta."""

        payload = _require_keys(_decode_payload(data, kind="delta"), _DELTA_KEYS, kind="delta")
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("delta-invalid-schema")
        base_digest = _require_digest(payload["base_digest"], field="base_digest")
        target_digest = _require_digest(payload["target_digest"], field="target_digest")
        prefix_items = _require_prefix(payload["prefix_items"])
        appended = _decode_items(payload["appended"], field="appended")
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix_items,
            appended=appended,
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild the target pack from ``base`` and ``delta``.

    The rebuilt pack is verified against the delta's target digest, so a delta
    that does not describe the target it claims is rejected instead of
    silently producing a different body.
    """

    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("pack-invalid-arguments")
    if delta.base_digest != base.digest:
        raise ContextPackError("delta-base-digest-mismatch")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta-prefix-out-of-range")
    target = ContextPack.build((*base.items[: delta.prefix_items], *delta.appended))
    if target.digest != delta.target_digest:
        raise ContextPackError("delta-target-digest-mismatch")
    return target


#: Chunk boundaries used by prepared bodies, in bytes from the start of the body.
#:
#: The leading window is cut finely because that is where two consecutive
#: prepared bodies usually stop matching: the trust preamble, the markers and
#: the start of the JSON envelope are shared, and the first citation, ranking
#: order or memory revision ends the shared prefix after a few hundred bytes.
#: Cutting on a fine grid there lets the delta keep that shared head instead of
#: resending it. Past the window the grid widens, because a body that already
#: diverged is resent in bulk anyway.
_FINE_GRID_LIMIT = 1024
_FINE_GRID_STEP = 64


def _next_boundary(position: int) -> int:
    """Return the first chunk boundary strictly after ``position``."""

    fine = -(-(position + 1) // _FINE_GRID_STEP) * _FINE_GRID_STEP
    if fine <= _FINE_GRID_LIMIT:
        return fine
    return _FINE_GRID_LIMIT + -(-(position + 1 - _FINE_GRID_LIMIT) // DEFAULT_CHUNK_BYTES) * DEFAULT_CHUNK_BYTES


def _utf8_char_length(lead: int) -> int:
    """Return the byte length of the UTF-8 character starting with ``lead``."""

    if lead < 0x80:
        return 1
    if lead < 0xE0:
        return 2
    if lead < 0xF0:
        return 3
    return 4


def _grid_chunks(body: bytes, *, start: int = 0) -> tuple[bytes, ...]:
    """Split ``body[start:]`` on the prepared-body chunk grid.

    Every chunk is valid UTF-8 on its own and no chunk exceeds
    :data:`DEFAULT_CHUNK_BYTES`. Boundaries that fall inside a multi-byte
    character move back to the previous character edge.
    """

    if start >= len(body):
        return ()
    boundaries: list[int] = []
    position = start
    while position < len(body):
        boundary = min(_next_boundary(position), len(body))
        while boundary > position and boundary < len(body) and (body[boundary] & 0xC0) == 0x80:
            boundary -= 1
        if boundary == position:
            boundary = position + _utf8_char_length(body[position])
        boundaries.append(boundary)
        position = boundary
    chunks: list[bytes] = []
    previous = start
    for boundary in boundaries:
        chunks.append(body[previous:boundary])
        previous = boundary
    return tuple(chunks)


def prepared_pack(content: str | None) -> ContextPack:
    """Build the pack of one prepared context body on the prepared-body grid."""

    if content is None:
        return ContextPack.build(())
    if isinstance(content, (bytes, bytearray, memoryview)):
        body = bytes(content)
    elif isinstance(content, str):
        body = content.encode("utf-8")
    else:
        raise ContextPackError("pack-invalid-content")
    return ContextPack.build(_grid_chunks(body))


def aligned_pack(base: ContextPack, content: str | None) -> ContextPack:
    """Build the pack of ``content`` so that it reuses the base pack's chunks.

    The leading chunks of the result are the base pack's own chunks for as long
    as they still match the body byte for byte. Only the part of the body after
    that shared prefix is split anew, so a delta against this pack carries only
    what actually changed.
    """

    body = b"" if content is None else content.encode("utf-8") if isinstance(content, str) else bytes(content)
    base_body = b"".join(base.items)
    shared_bytes = 0
    limit = min(len(body), len(base_body))
    while shared_bytes < limit and body[shared_bytes] == base_body[shared_bytes]:
        shared_bytes += 1
    kept = 0
    kept_bytes = 0
    for item in base.items:
        if kept_bytes + len(item) > shared_bytes:
            break
        kept_bytes += len(item)
        kept += 1
    prefix = base.items[:kept]
    try:
        body[kept_bytes:].decode("utf-8")
    except UnicodeDecodeError:
        return prepared_pack(content)
    return ContextPack.build((*prefix, *_grid_chunks(body, start=kept_bytes)))
