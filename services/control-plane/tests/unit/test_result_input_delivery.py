from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.ports import ResultInputContentPort
from sidera_control_plane.application.result_input_delivery import (
    ResultInputDeliveryError,
    materialize_result_inputs,
)
from sidera_control_plane.domain.context_packages import (
    ResultInputBudgetV1,
    ResultInputManifestV1,
)
from tests.unit.result_input_fixtures import result_input_manifest

pytestmark = pytest.mark.unit

WORKSPACE_ID = UUID("61000000-0000-0000-0000-000000000002")


class MemoryResultInputContentPort(ResultInputContentPort):
    def __init__(self, contents: dict[UUID, bytes]) -> None:
        self.contents = contents
        self.calls: list[tuple[UUID, UUID, str, int]] = []

    def open_exact(
        self,
        *,
        workspace_id: UUID,
        artifact_id: UUID,
        expected_hash: str,
        expected_size_bytes: int,
    ) -> BinaryIO:
        self.calls.append(
            (workspace_id, artifact_id, expected_hash, expected_size_bytes)
        )
        if artifact_id not in self.contents:
            raise FileNotFoundError(str(artifact_id))
        return BytesIO(self.contents[artifact_id])


def result_input_manifest_for_body(body: bytes) -> ResultInputManifestV1:
    base = result_input_manifest()
    artifact_id = base.artifacts[0].artifact_id
    raw = deepcopy(base.to_mapping())
    artifacts = raw["artifacts"]
    assert isinstance(artifacts, list)
    selected_artifact = next(
        item for item in artifacts if isinstance(item, dict) and item["artifact_id"] == str(artifact_id)
    )
    selected_artifact["content_hash"] = hashlib.sha256(body).hexdigest()
    selected_artifact["size_bytes"] = len(body)
    raw["artifacts"] = [selected_artifact]
    evidence = raw["evidence"]
    assert isinstance(evidence, list)
    raw["evidence"] = [
        item
        for item in evidence
        if isinstance(item, dict) and str(artifact_id) in item["artifact_ids"]
    ]
    return ResultInputManifestV1.from_mapping(raw)


def result_input_manifest_for_bodies(
    bodies: tuple[bytes, ...], *, budget: ResultInputBudgetV1 | None = None
) -> tuple[ResultInputManifestV1, dict[UUID, bytes]]:
    base = result_input_manifest()
    contents: dict[UUID, bytes] = {}
    artifacts = []
    for body in bodies:
        artifact_id = uuid4()
        contents[artifact_id] = body
        artifacts.append(
            replace(
                base.artifacts[0],
                artifact_id=artifact_id,
                content_hash=hashlib.sha256(body).hexdigest(),
                size_bytes=len(body),
            )
        )
    manifest = replace(
        base,
        artifacts=tuple(artifacts),
        evidence=(),
        budget=budget or base.budget,
    )
    return manifest, contents


def test_materialization_preserves_exact_utf8_bytes() -> None:
    body = "结论\r\nline two\n".encode()
    manifest = result_input_manifest_for_body(body)
    port = MemoryResultInputContentPort({manifest.artifacts[0].artifact_id: body})

    values = materialize_result_inputs(
        workspace_id=WORKSPACE_ID,
        manifest=manifest,
        content_port=port,
    )

    assert values[0].body_text.encode() == body
    assert values[0].content_hash == hashlib.sha256(body).hexdigest()
    assert values[0].size_bytes == len(body)
    assert values[0].group_keys == ("research",)
    assert values[0].candidate_evidence
    assert port.calls == [
        (
            WORKSPACE_ID,
            manifest.artifacts[0].artifact_id,
            manifest.artifacts[0].content_hash,
            len(body),
        )
    ]


@pytest.mark.parametrize("body", [b"", b"\xef\xbb\xbfline\r\n"])
def test_empty_and_bom_utf8_bodies_are_preserved(body: bytes) -> None:
    manifest = result_input_manifest_for_body(body)
    port = MemoryResultInputContentPort({manifest.artifacts[0].artifact_id: body})

    values = materialize_result_inputs(
        workspace_id=WORKSPACE_ID,
        manifest=manifest,
        content_port=port,
    )

    assert values[0].body_text.encode() == body


def test_missing_content_is_retryable_and_does_not_leak_storage_reference() -> None:
    manifest = result_input_manifest_for_body(b"missing\n")
    port = MemoryResultInputContentPort({})

    with pytest.raises(ResultInputDeliveryError) as error_info:
        materialize_result_inputs(
            workspace_id=WORKSPACE_ID,
            manifest=manifest,
            content_port=port,
        )

    assert error_info.value.code == "result_input_unavailable"
    assert error_info.value.temporary is True
    assert "storage_ref" not in str(error_info.value)


@pytest.mark.parametrize("actual", [b"other\n", b"short", b"longer-than-declared\n"])
def test_hash_size_or_truncated_content_is_corrupt(actual: bytes) -> None:
    manifest = result_input_manifest_for_body(b"expected\n")
    port = MemoryResultInputContentPort({manifest.artifacts[0].artifact_id: actual})

    with pytest.raises(ResultInputDeliveryError) as error_info:
        materialize_result_inputs(
            workspace_id=WORKSPACE_ID,
            manifest=manifest,
            content_port=port,
        )

    assert error_info.value.code == "result_input_integrity_failed"
    assert error_info.value.corrupt is True
    assert "expected\n" not in str(error_info.value)


def test_invalid_utf8_is_not_marked_as_storage_corruption() -> None:
    body = b"\xff\xfe"
    manifest = result_input_manifest_for_body(body)
    port = MemoryResultInputContentPort({manifest.artifacts[0].artifact_id: body})

    with pytest.raises(ResultInputDeliveryError) as error_info:
        materialize_result_inputs(
            workspace_id=WORKSPACE_ID,
            manifest=manifest,
            content_port=port,
        )

    assert error_info.value.code == "result_input_format_unsupported"
    assert error_info.value.corrupt is False


def test_unsupported_media_type_is_rejected_before_content_read() -> None:
    base = result_input_manifest_for_body(b"binary\n")
    raw = base.to_mapping()
    raw["artifacts"][0]["media_type"] = "application/octet-stream"  # type: ignore[index]
    manifest = ResultInputManifestV1.from_mapping(raw)
    port = MemoryResultInputContentPort({manifest.artifacts[0].artifact_id: b"binary\n"})

    with pytest.raises(ResultInputDeliveryError) as error_info:
        materialize_result_inputs(
            workspace_id=WORKSPACE_ID,
            manifest=manifest,
            content_port=port,
        )

    assert error_info.value.code == "result_input_format_unsupported"
    assert port.calls == []


def test_single_artifact_limit_is_checked_before_read() -> None:
    manifest = result_input_manifest_for_body(b"x" * 32_769)
    port = MemoryResultInputContentPort({manifest.artifacts[0].artifact_id: b"x" * 32_769})

    with pytest.raises(ResultInputDeliveryError, match="single artifact") as error_info:
        materialize_result_inputs(
            workspace_id=WORKSPACE_ID,
            manifest=manifest,
            content_port=port,
        )

    assert error_info.value.code == "result_input_budget_exceeded"
    assert port.calls == []


def test_single_artifact_limit_is_inclusive() -> None:
    body = b"x" * 32_768
    manifest = result_input_manifest_for_body(body)
    port = MemoryResultInputContentPort({manifest.artifacts[0].artifact_id: body})

    values = materialize_result_inputs(
        workspace_id=WORKSPACE_ID,
        manifest=manifest,
        content_port=port,
    )

    assert values[0].size_bytes == 32_768


def test_total_artifact_limit_is_checked_before_read() -> None:
    base = result_input_manifest()
    budget = replace(
        base.budget,
        max_artifact_bytes=16,
        max_total_artifact_bytes=17,
        max_payload_bytes=17,
    )
    manifest = replace(base, budget=budget)
    port = MemoryResultInputContentPort(
        {
            base.artifacts[0].artifact_id: b"report-a\n",
            base.artifacts[1].artifact_id: b"report-b\n",
        }
    )

    with pytest.raises(ResultInputDeliveryError, match="total artifact") as error_info:
        materialize_result_inputs(
            workspace_id=WORKSPACE_ID,
            manifest=manifest,
            content_port=port,
        )

    assert error_info.value.code == "result_input_budget_exceeded"
    assert port.calls == []


@pytest.mark.parametrize(
    ("bodies", "expected_count"),
    [
        ((b"x" * 32_768,) * 4, 4),
        ((b"x" * 32_768,) * 4 + (b"x",), 0),
    ],
)
def test_total_artifact_limit_is_131072_inclusive(
    bodies: tuple[bytes, ...], expected_count: int
) -> None:
    budget = ResultInputBudgetV1(
        max_artifact_bytes=32_768,
        max_total_artifact_bytes=131_072,
        max_artifact_count=128,
        max_payload_bytes=262_144,
    )
    manifest, contents = result_input_manifest_for_bodies(bodies, budget=budget)
    port = MemoryResultInputContentPort(contents)

    if expected_count == 0:
        with pytest.raises(ResultInputDeliveryError) as error_info:
            materialize_result_inputs(
                workspace_id=WORKSPACE_ID,
                manifest=manifest,
                content_port=port,
            )
        assert error_info.value.code == "result_input_budget_exceeded"
        assert port.calls == []
        return

    values = materialize_result_inputs(
        workspace_id=WORKSPACE_ID,
        manifest=manifest,
        content_port=port,
    )
    assert len(values) == expected_count


def test_artifact_count_limit_is_checked_before_read() -> None:
    base = result_input_manifest()
    descriptors = tuple(
        replace(
            base.artifacts[0],
            artifact_id=uuid4(),
            content_hash=hashlib.sha256(b"x").hexdigest(),
            size_bytes=1,
        )
        for _ in range(129)
    )
    manifest = replace(base, artifacts=descriptors, evidence=())
    port = MemoryResultInputContentPort({})

    with pytest.raises(ResultInputDeliveryError, match="artifact count") as error_info:
        materialize_result_inputs(
            workspace_id=WORKSPACE_ID,
            manifest=manifest,
            content_port=port,
        )

    assert error_info.value.code == "result_input_budget_exceeded"
    assert port.calls == []


def test_artifact_count_limit_is_128_inclusive() -> None:
    bodies = (b"x",) * 128
    manifest, contents = result_input_manifest_for_bodies(bodies)
    port = MemoryResultInputContentPort(contents)

    values = materialize_result_inputs(
        workspace_id=WORKSPACE_ID,
        manifest=manifest,
        content_port=port,
    )

    assert len(values) == 128
