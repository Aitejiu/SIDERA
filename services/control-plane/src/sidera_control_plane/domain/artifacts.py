import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError


class ArtifactType(StrEnum):
    DIFF = "diff"
    LOG = "log"
    REPORT = "report"
    SCREENSHOT = "screenshot"
    BUILD = "build"
    EXTERNAL = "external"


class EvidenceClaimStatus(StrEnum):
    CANDIDATE = "candidate"


@dataclass(frozen=True, slots=True)
class ArtifactCandidate:
    artifact_type: ArtifactType
    storage_ref: str
    content_hash: str
    size_bytes: int
    media_type: str
    provenance: Mapping[str, Any]
    content: bytes | None = None

    def __post_init__(self) -> None:
        if not self.storage_ref.strip() or not self.media_type.strip():
            raise ValidationError("artifact storage_ref and media_type are required")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", self.content_hash):
            raise ValidationError("artifact content_hash must be a SHA-256 hex digest")
        if self.size_bytes < 0:
            raise ValidationError("artifact size_bytes cannot be negative")
        if self.content is not None and not isinstance(self.content, bytes):
            raise ValidationError("artifact content must be bytes")
        object.__setattr__(
            self,
            "artifact_type",
            _artifact_type(self.artifact_type),
        )
        if not isinstance(self.provenance, Mapping):
            raise ValidationError("artifact provenance must be a mapping")
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))


@dataclass(frozen=True, slots=True)
class Artifact:
    id: UUID
    workspace_id: UUID
    task_id: UUID
    execution_id: UUID
    session_id: UUID | None
    artifact_type: ArtifactType
    storage_ref: str
    content_hash: str
    size_bytes: int
    media_type: str
    provenance: Mapping[str, Any]

    @classmethod
    def from_candidate(
        cls,
        *,
        id: UUID,
        workspace_id: UUID,
        task_id: UUID,
        execution_id: UUID,
        session_id: UUID | None,
        candidate: ArtifactCandidate,
    ) -> "Artifact":
        return cls(
            id=id,
            workspace_id=workspace_id,
            task_id=task_id,
            execution_id=execution_id,
            session_id=session_id,
            artifact_type=candidate.artifact_type,
            storage_ref=candidate.storage_ref,
            content_hash=candidate.content_hash,
            size_bytes=candidate.size_bytes,
            media_type=candidate.media_type,
            provenance=candidate.provenance,
        )


@dataclass(frozen=True, slots=True)
class EvidenceClaim:
    id: UUID
    workspace_id: UUID
    task_id: UUID
    execution_id: UUID
    acceptance_criterion_ref: str
    conclusion: str
    environment_ref: str
    artifact_ids: tuple[UUID, ...]
    producer: str
    status: EvidenceClaimStatus
    created_at: Any

    @classmethod
    def create_candidate(
        cls,
        *,
        id: UUID,
        workspace_id: UUID,
        task_id: UUID,
        execution_id: UUID,
        acceptance_criterion_ref: str,
        conclusion: str,
        environment_ref: str,
        artifact_ids: tuple[UUID, ...],
        producer: str,
        created_at: Any,
    ) -> "EvidenceClaim":
        for name, value in (
            ("acceptance_criterion_ref", acceptance_criterion_ref),
            ("conclusion", conclusion),
            ("environment_ref", environment_ref),
            ("producer", producer),
        ):
            if not value.strip():
                raise ValidationError(f"{name} is required")
        if not artifact_ids:
            raise ValidationError("evidence claim requires an artifact")
        return cls(
            id=id,
            workspace_id=workspace_id,
            task_id=task_id,
            execution_id=execution_id,
            acceptance_criterion_ref=acceptance_criterion_ref.strip(),
            conclusion=conclusion.strip(),
            environment_ref=environment_ref.strip(),
            artifact_ids=tuple(artifact_ids),
            producer=producer.strip(),
            status=EvidenceClaimStatus.CANDIDATE,
            created_at=created_at,
        )


def _artifact_type(value: ArtifactType | str) -> ArtifactType:
    try:
        return value if isinstance(value, ArtifactType) else ArtifactType(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported artifact type: {value!r}") from exc
