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

"""Source lifecycle bindings, availability propagation, and reference resolution.

Each Scope owns one dependency graph of Sources, Memories, and Topic Memories.
Withdrawing a Source makes the content that lost its support unavailable for
search, Context preparation, and generation while its body and its historical
references stay readable, and restoring the Source makes the supported content
available again.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationInfo, field_validator

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryCitation, MemoryService
from powercontext.builtin.artifacts.topic_memory import TopicMemory
from powercontext.builtin.persistence.source_lifecycle import (
    SOURCE_LIFECYCLE_KIND_MEMORY,
    SOURCE_LIFECYCLE_KIND_SOURCE,
    SOURCE_LIFECYCLE_KIND_TOPIC,
    SOURCE_LIFECYCLE_POLICY_ALL,
    SOURCE_LIFECYCLE_POLICY_ANY,
    SourceLifecycleNode,
    SourceLifecycleStore,
    decode_reference,
    encode_reference,
    validate_node_id,
)
from powercontext.builtin.records import BaseAccessError
from powercontext.builtin.sources import validate_scope_id
from powercontext.builtin.statistics import ModelUsagePurpose
from powercontext.errors import ArtifactNotFoundError, PowerContextError
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.context import BuiltinArtifacts
    from powercontext.builtin.runtime.application import BuiltinRuntime

SOURCE_LIFECYCLE_PURPOSE_GENERATION = "generation"
SOURCE_LIFECYCLE_PURPOSE_AUDIT = "audit"
SOURCE_LIFECYCLE_PURPOSES = (SOURCE_LIFECYCLE_PURPOSE_GENERATION, SOURCE_LIFECYCLE_PURPOSE_AUDIT)

_REFERENCE_MODELS: dict[str, type[Any]] = {
    SOURCE_LIFECYCLE_KIND_SOURCE: SourceRef,
    SOURCE_LIFECYCLE_KIND_MEMORY: MemoryCitation,
    SOURCE_LIFECYCLE_KIND_TOPIC: ArtifactRef,
}
_MEMORY_REASON = "source lifecycle availability"
_FIELD_SEPARATOR = "\x00"


class SourceLifecycleError(ValueError):
    """A rejected lifecycle registration, event, or resolution."""


class SourceLifecycleEvent(BaseModel):
    """One caller-supplied availability event for a registered Source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    revision: StrictInt = Field(gt=0)
    sequence: StrictInt = Field(gt=0)
    available: StrictBool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def reject_blank(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise SourceLifecycleError(f"{info.field_name} must not be blank")
        return value


class SourceLifecycleApplication:
    """Source lifecycle services, selected by Scope."""

    def __init__(self, runtime: BuiltinRuntime) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleService:
        """Return the lifecycle service bound to one Scope."""

        return ScopedSourceLifecycleService(self._runtime, scope_id)


class ScopedSourceLifecycleService:
    """Register evidence nodes, apply Source events, and resolve references."""

    def __init__(self, runtime: BuiltinRuntime, scope_id: str, /) -> None:
        self._runtime = runtime
        self.scope_id = validate_scope_id(scope_id)

    async def register_source(self, node_id: str, source_ref: SourceRef, /, *, revision: int) -> None:
        """Bind one registered node to a captured Source at an exact lifecycle revision."""

        node = _require_node_id(node_id)
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("register_source requires a SourceRef")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise SourceLifecycleError("register_source requires a positive revision")
        await self._register(
            node,
            kind=SOURCE_LIFECYCLE_KIND_SOURCE,
            reference=source_ref,
            parents=(),
            policy=SOURCE_LIFECYCLE_POLICY_ALL,
            source_revision=revision,
            artifact_id=None,
        )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        """Bind one registered node to an exact Memory entry version."""

        node = _require_node_id(node_id)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("register_memory requires a MemoryCitation")
        await self._register(
            node,
            kind=SOURCE_LIFECYCLE_KIND_MEMORY,
            reference=citation,
            parents=_require_parents(node, parents),
            policy=_require_policy(policy),
            source_revision=None,
            artifact_id=citation.memory_ref.artifact_id,
        )

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        """Bind one registered node to an exact Topic Memory Artifact revision."""

        node = _require_node_id(node_id)
        if not isinstance(artifact_ref, ArtifactRef) or artifact_ref.family != TopicMemory.family:
            raise SourceLifecycleError("register_topic requires a topic-memory ArtifactRef")
        await self._register(
            node,
            kind=SOURCE_LIFECYCLE_KIND_TOPIC,
            reference=artifact_ref,
            parents=_require_parents(node, parents),
            policy=_require_policy(policy),
            source_revision=None,
            artifact_id=artifact_ref.artifact_id,
        )

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        """Apply one Source event and return every available node id in order."""

        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("apply requires a SourceLifecycleEvent")
        digest = _event_digest(event)
        async with self._runtime._scoped_operation(self.scope_id):
            async with self._runtime._locked(self.scope_id):
                nodes = await self._store().list_nodes(self.scope_id)
                target = next((node for node in nodes if node.node_id == event.node_id), None)
                if target is None:
                    raise SourceLifecycleError(f"unknown node {event.node_id!r}")
                if target.kind != SOURCE_LIFECYCLE_KIND_SOURCE:
                    raise SourceLifecycleError(f"node {event.node_id!r} is not a Source")
                if target.source_revision != event.revision:
                    raise SourceLifecycleError(
                        f"node {event.node_id!r} is registered at revision {target.source_revision}"
                    )
                if target.sequence is not None and event.sequence < target.sequence:
                    raise SourceLifecycleError(f"node {event.node_id!r} already applied sequence {target.sequence}")
                if target.sequence == event.sequence and target.event_digest != digest:
                    raise SourceLifecycleError(f"node {event.node_id!r} already applied sequence {event.sequence}")
                available = {node.node_id: node.available for node in nodes}
                changes: list[SourceLifecycleNode] = []
                consumed = target.sequence == event.sequence and target.event_digest == digest
                if not consumed or target.available != event.available:
                    available[target.node_id] = event.available
                    changes.append(
                        replace(target, available=event.available, sequence=event.sequence, event_digest=digest)
                    )
                for node in nodes:
                    if node.kind == SOURCE_LIFECYCLE_KIND_SOURCE:
                        continue
                    supported = _supported(node.policy, tuple(available[parent] for parent in node.parents))
                    if supported != available[node.node_id]:
                        available[node.node_id] = supported
                        changes.append(replace(node, available=supported))
                if changes:
                    await self._store().save_nodes(self.scope_id, tuple(changes))
            await self._converge_memory()
        return sorted(node_id for node_id, value in available.items() if value)

    async def resolve(self, node_id: str, /, *, purpose: str) -> SourceRef | MemoryCitation | ArtifactRef:
        """Return the registered reference of one node for one usage purpose."""

        if purpose not in SOURCE_LIFECYCLE_PURPOSES:
            raise SourceLifecycleError(f"unknown purpose {purpose!r}")
        nodes = await self._store().list_nodes(self.scope_id)
        node = next((candidate for candidate in nodes if candidate.node_id == node_id), None)
        if node is None:
            raise SourceLifecycleError(f"unknown node {node_id!r}")
        if purpose == SOURCE_LIFECYCLE_PURPOSE_GENERATION and not node.available:
            raise SourceLifecycleError(f"node {node_id!r} is not available for generation")
        return decode_reference(_REFERENCE_MODELS[node.kind], node.reference, node_id=node.node_id)

    def _store(self) -> SourceLifecycleStore:
        return self._runtime._source_lifecycle

    async def _register(
        self,
        node_id: str,
        *,
        kind: str,
        reference: SourceRef | MemoryCitation | ArtifactRef,
        parents: tuple[str, ...],
        policy: str,
        source_revision: int | None,
        artifact_id: str | None,
    ) -> None:
        payload = encode_reference(reference, node_id=node_id)
        async with self._runtime._context(
            self.scope_id, embedding_purpose=ModelUsagePurpose.MEMORY_INDEXING
        ) as context:
            await self._require_reference(context.artifacts, kind, reference)
            async with self._runtime._locked(self.scope_id):
                nodes = await self._store().list_nodes(self.scope_id)
                registered = {node.node_id: node for node in nodes}
                current = registered.get(node_id)
                if current is not None:
                    if (
                        current.kind == kind
                        and current.reference == payload
                        and current.parents == parents
                        and current.policy == policy
                        and current.source_revision == source_revision
                    ):
                        return
                    raise SourceLifecycleError(f"node {node_id!r} is already registered with a different binding")
                _require_edges(node_id, parents, registered)
                record = SourceLifecycleNode(
                    node_id=node_id,
                    ordinal=len(nodes),
                    kind=kind,
                    policy=policy,
                    reference=payload,
                    parents=parents,
                    source_revision=source_revision,
                    sequence=None,
                    event_digest=None,
                    artifact_id=artifact_id,
                    available=kind == SOURCE_LIFECYCLE_KIND_SOURCE
                    or _supported(policy, tuple(registered[parent].available for parent in parents)),
                )
                await self._store().save_nodes(self.scope_id, (record,))
                if kind == SOURCE_LIFECYCLE_KIND_MEMORY:
                    await self._apply_memory_availability(context.artifacts, (record,))

    async def _require_reference(
        self,
        artifacts: BuiltinArtifacts,
        kind: str,
        reference: SourceRef | MemoryCitation | ArtifactRef,
    ) -> None:
        """Reject a binding whose reference cannot be read in this Scope."""

        if kind == SOURCE_LIFECYCLE_KIND_MEMORY:
            assert isinstance(reference, MemoryCitation)
            try:
                await artifacts.memory.validate_citation(reference)
            except PowerContextError:
                raise SourceLifecycleError("binding reference memory is not readable") from None
            return
        records = self._runtime._records()
        try:
            if kind == SOURCE_LIFECYCLE_KIND_SOURCE:
                assert isinstance(reference, SourceRef)
                await records.get_source(self.scope_id, reference.source_type, reference.source_id)
                return
            assert isinstance(reference, ArtifactRef)
            await records.get_artifact_revision(
                self.scope_id, reference.family, reference.artifact_id, reference.revision
            )
        except (PowerContextError, BaseAccessError):
            raise SourceLifecycleError(f"binding reference {kind} is not readable") from None

    async def _converge_memory(self) -> None:
        """Drive native Memory availability to the persisted lifecycle state."""

        nodes = tuple(
            node
            for node in await self._store().list_nodes(self.scope_id)
            if node.kind == SOURCE_LIFECYCLE_KIND_MEMORY
        )
        if not nodes:
            return
        async with self._runtime._context(
            self.scope_id, embedding_purpose=ModelUsagePurpose.MEMORY_INDEXING
        ) as context:
            async with self._runtime._locked(self.scope_id):
                await self._apply_memory_availability(context.artifacts, nodes)

    async def _apply_memory_availability(
        self,
        artifacts: BuiltinArtifacts,
        nodes: tuple[SourceLifecycleNode, ...],
    ) -> None:
        """Keep native Memory entry states and the lifecycle graph consistent.

        Every state change is idempotent, so a propagation that a failed Memory write
        interrupted converges once the same transition is applied again.
        """

        service: MemoryService = artifacts.memory
        memory = await _head_or_none(service, artifacts.memory_artifact_id)
        if memory is None:
            return
        entries = {entry.entry_id: entry for entry in await service.entries(memory)}
        states = {item.entry_id: item.state for item in memory.content.manifest.entries}
        desired: dict[str, bool] = {}
        for node in nodes:
            citation = decode_reference(MemoryCitation, node.reference, node_id=node.node_id)
            if citation.entry_id not in entries:
                continue
            desired[citation.entry_id] = desired.get(citation.entry_id, False) or node.available
        withdrawn = tuple(
            entry
            for entry_id, entry in entries.items()
            if entry_id in desired and not desired[entry_id] and states.get(entry_id) != "inactive"
        )
        restored = tuple(
            entry
            for entry_id, entry in entries.items()
            if desired.get(entry_id, False) and states.get(entry_id) != "active"
        )
        if withdrawn:
            memory = await service.forget(memory, entries=withdrawn, reason=_MEMORY_REASON)
        if restored:
            await service.reactivate(memory, entries=restored, reason=_MEMORY_REASON)


def _require_node_id(node_id: str) -> str:
    try:
        return validate_node_id(node_id)
    except ValueError:
        raise SourceLifecycleError(f"invalid node id {node_id!r}") from None


def _require_parents(node_id: str, parents: Sequence[str]) -> tuple[str, ...]:
    if isinstance(parents, str) or not isinstance(parents, Sequence):
        raise SourceLifecycleError(f"node {node_id!r} requires a sequence of parent ids")
    edges = tuple(parents)
    if not edges:
        raise SourceLifecycleError(f"node {node_id!r} requires at least one parent")
    if any(not isinstance(parent, str) for parent in edges):
        raise SourceLifecycleError(f"node {node_id!r} requires string parent ids")
    if len(set(edges)) != len(edges):
        raise SourceLifecycleError(f"node {node_id!r} repeats a parent")
    if node_id in edges:
        raise SourceLifecycleError(f"node {node_id!r} cannot depend on itself")
    return edges


def _require_policy(policy: str) -> str:
    if policy not in (SOURCE_LIFECYCLE_POLICY_ALL, SOURCE_LIFECYCLE_POLICY_ANY):
        raise SourceLifecycleError(f"unknown dependency policy {policy!r}")
    return policy


def _require_edges(node_id: str, parents: tuple[str, ...], registered: dict[str, SourceLifecycleNode]) -> None:
    """Reject edges to nodes that are not registered yet.

    Parents always precede their derived nodes, so every accepted graph is acyclic.
    """

    if not parents:
        return
    for parent in parents:
        if parent not in registered:
            raise SourceLifecycleError(f"node {node_id!r} depends on unregistered node {parent!r}")


def _supported(policy: str, parents: tuple[bool, ...]) -> bool:
    if not parents:
        return False
    return all(parents) if policy == SOURCE_LIFECYCLE_POLICY_ALL else any(parents)


def _event_digest(event: SourceLifecycleEvent) -> str:
    parts = (event.node_id, str(event.revision), str(event.sequence), "1" if event.available else "0")
    return hashlib.sha256(_FIELD_SEPARATOR.join(parts).encode()).hexdigest()


async def _head_or_none(service: MemoryService, artifact_id: str) -> Any:
    try:
        return await service.head(artifact_id)
    except ArtifactNotFoundError:
        return None


__all__ = (
    "SOURCE_LIFECYCLE_PURPOSES",
    "SOURCE_LIFECYCLE_PURPOSE_AUDIT",
    "SOURCE_LIFECYCLE_PURPOSE_GENERATION",
    "ScopedSourceLifecycleService",
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
)
