from __future__ import annotations

import hashlib
import json
from typing import BinaryIO
from uuid import UUID

from sidera_control_plane.application.artifact_content import (
    ArtifactContentUnavailableError,
    ArtifactIntegrityFailed,
    ArtifactMediaTypeUnsupported,
)
from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.ports import ResultInputContentPort
from sidera_control_plane.domain.context_packages import (
    MaterializedResultInput,
    ResultInputEvidenceManifestV1,
    ResultInputManifestV1,
)

_TEXT_MEDIA_TYPES = frozenset({"text/plain", "text/x-diff", "application/json"})


class ResultInputDeliveryError(RuntimeError):
    """A selected result body cannot be delivered safely to a consumer."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        temporary: bool = False,
        corrupt: bool = False,
    ) -> None:
        self.code = code
        self.temporary = temporary
        self.corrupt = corrupt
        super().__init__(message)


def materialize_result_inputs(
    *,
    workspace_id: UUID,
    manifest: ResultInputManifestV1,
    content_port: ResultInputContentPort,
) -> tuple[MaterializedResultInput, ...]:
    """Read only the exact, bounded artifacts authorized by a result manifest."""

    if not isinstance(workspace_id, UUID):
        raise ResultInputDeliveryError(
            "result_input_unavailable",
            "Result input workspace is invalid",
            temporary=False,
        )
    if not isinstance(manifest, ResultInputManifestV1):
        raise ResultInputDeliveryError(
            "result_input_integrity_failed",
            "Result input manifest is invalid",
            corrupt=True,
        )

    budget = manifest.budget
    artifact_count = len(manifest.artifacts)
    if artifact_count > budget.max_artifact_count:
        raise ResultInputDeliveryError(
            "result_input_budget_exceeded",
            "Result input artifact count exceeds the fixed budget",
        )
    total_artifact_bytes = sum(item.size_bytes for item in manifest.artifacts)
    if any(item.size_bytes > budget.max_artifact_bytes for item in manifest.artifacts):
        raise ResultInputDeliveryError(
            "result_input_budget_exceeded",
            "Result input single artifact exceeds the fixed budget",
        )
    if total_artifact_bytes > budget.max_total_artifact_bytes:
        raise ResultInputDeliveryError(
            "result_input_budget_exceeded",
            "Result input total artifact bytes exceed the fixed budget",
        )

    evidence_by_artifact: dict[UUID, list[ResultInputEvidenceManifestV1]] = {}
    for evidence in manifest.evidence:
        for artifact_id in evidence.artifact_ids:
            evidence_by_artifact.setdefault(artifact_id, []).append(evidence)

    values: list[MaterializedResultInput] = []
    for descriptor in manifest.artifacts:
        if descriptor.media_type not in _TEXT_MEDIA_TYPES:
            raise ResultInputDeliveryError(
                "result_input_format_unsupported",
                "Selected result input media type is not supported as text",
            )
        stream = _open_content(
            content_port,
            workspace_id=workspace_id,
            artifact_id=descriptor.artifact_id,
            expected_hash=descriptor.content_hash,
            expected_size_bytes=descriptor.size_bytes,
        )
        try:
            body = _read_bounded(stream, descriptor.size_bytes + 1)
        except ResultInputDeliveryError:
            raise
        except (OSError, ValueError) as exc:
            raise ResultInputDeliveryError(
                "result_input_unavailable",
                "Selected result input could not be read",
                temporary=True,
            ) from exc
        except Exception as exc:
            raise ResultInputDeliveryError(
                "result_input_unavailable",
                "Selected result input could not be read",
                temporary=True,
            ) from exc
        finally:
            try:
                stream.close()
            except OSError:
                pass

        if (
            len(body) != descriptor.size_bytes
            or hashlib.sha256(body).hexdigest() != descriptor.content_hash
        ):
            raise ResultInputDeliveryError(
                "result_input_integrity_failed",
                "Selected result input length or hash does not match its manifest",
                corrupt=True,
            )
        try:
            body_text = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ResultInputDeliveryError(
                "result_input_format_unsupported",
                "Selected result input is not valid UTF-8 text",
            ) from exc
        values.append(
            MaterializedResultInput(
                artifact_id=descriptor.artifact_id,
                content_hash=descriptor.content_hash,
                size_bytes=descriptor.size_bytes,
                media_type=descriptor.media_type,
                sources=descriptor.sources,
                group_keys=descriptor.group_keys,
                hard_refs=descriptor.hard_refs,
                body_text=body_text,
                candidate_evidence=tuple(
                    evidence_by_artifact.get(descriptor.artifact_id, ())
                ),
            )
        )

    if _encoded_size(values) > budget.max_payload_bytes:
        raise ResultInputDeliveryError(
            "result_input_budget_exceeded",
            "Materialized result inputs exceed the fixed payload budget",
        )
    return tuple(values)


def _open_content(
    content_port: ResultInputContentPort,
    *,
    workspace_id: UUID,
    artifact_id: UUID,
    expected_hash: str,
    expected_size_bytes: int,
) -> BinaryIO:
    try:
        return content_port.open_exact(
            workspace_id=workspace_id,
            artifact_id=artifact_id,
            expected_hash=expected_hash,
            expected_size_bytes=expected_size_bytes,
        )
    except ResultInputDeliveryError:
        raise
    except ArtifactIntegrityFailed as exc:
        raise ResultInputDeliveryError(
            "result_input_integrity_failed",
            "Selected result input content failed integrity checks",
            corrupt=True,
        ) from exc
    except ArtifactMediaTypeUnsupported as exc:
        raise ResultInputDeliveryError(
            "result_input_format_unsupported",
            "Selected result input media type is not supported",
        ) from exc
    except (ArtifactContentUnavailableError, NotFound, FileNotFoundError, OSError) as exc:
        raise ResultInputDeliveryError(
            "result_input_unavailable",
            "Selected result input content is unavailable",
            temporary=True,
        ) from exc
    except Exception as exc:
        raise ResultInputDeliveryError(
            "result_input_unavailable",
            "Selected result input content could not be opened",
            temporary=True,
        ) from exc


def _read_bounded(stream: BinaryIO, limit: int) -> bytes:
    if limit < 1:
        raise ValueError("bounded result input read limit must be positive")
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = stream.read(remaining)
        if chunk == b"":
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise ResultInputDeliveryError(
                "result_input_integrity_failed",
                "Selected result input stream did not return bytes",
                corrupt=True,
            )
        if len(chunk) > remaining:
            raise ResultInputDeliveryError(
                "result_input_integrity_failed",
                "Selected result input stream exceeded the bounded read limit",
                corrupt=True,
            )
        value = bytes(chunk)
        if not value:
            break
        chunks.append(value)
        remaining -= len(value)
    return b"".join(chunks)


def _encoded_size(values: list[MaterializedResultInput]) -> int:
    try:
        encoded = json.dumps(
            [item.to_mapping() for item in values],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ResultInputDeliveryError(
            "result_input_integrity_failed",
            "Materialized result inputs cannot be canonicalized",
            corrupt=True,
        ) from exc
    return len(encoded)
