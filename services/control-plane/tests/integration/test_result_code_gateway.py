from __future__ import annotations

import pytest

from sidera_control_plane.application.code_result_delivery import resolve_code_inputs
from sidera_control_plane.application.worker_gateway import GatewayMessageError
from sidera_control_plane.domain.code_results import CodeInputManifestV1
from sidera_control_plane.domain.result_operations import ResultError

pytestmark = pytest.mark.integration


def test_code_receipt_is_required_before_start(result_scenario) -> None:
    result_scenario.seed_plan()
    result_scenario.source(
        "login",
        files={"login.py": b"ALLOW_EXPIRED = False\n"},
        commit=True,
    )
    result_scenario.source("b", files={"b.py": b"B = True\n"}, commit=True)
    result_scenario.source("c", files={"c.py": b"C = True\n"}, commit=True)

    result_scenario.code_provider_failure = "content_unavailable"
    result = result_scenario.drive()

    assert isinstance(result, GatewayMessageError), result
    assert result.code == "code_input_unavailable"
    assert result_scenario.code_materialization_attempts("summary") > 0
    assert result_scenario.adapter_start_count("summary") == 0


def test_code_manifest_cannot_replace_snapshot_metadata(result_scenario) -> None:
    result_scenario.seed_plan()
    result_scenario.source(
        "login",
        files={"login.py": b"ALLOW_EXPIRED = False\n"},
        commit=True,
    )
    result_scenario.source("b", files={"b.py": b"B = True\n"}, commit=True)
    result_scenario.source("c", files={"c.py": b"C = True\n"}, commit=True)
    manifest = result_scenario.code_input_manifest()
    forged_mapping = manifest.to_mapping()
    forged_mapping["sources"] = [
        {**manifest.sources[0].to_mapping(), "result_hash": "0" * 64},
        *(item.to_mapping() for item in manifest.sources[1:]),
    ]
    forged_mapping["input_hash"] = _input_hash(forged_mapping)
    forged = CodeInputManifestV1.from_mapping(forged_mapping)

    with pytest.raises(ResultError, match="code_input_result_mismatch"), result_scenario.uow() as uow:
        resolve_code_inputs(uow, forged)


def test_legacy_worker_protocol_is_rejected_before_code_start(result_scenario) -> None:
    result_scenario.seed_plan()
    result_scenario.source(
        "login",
        files={"login.py": b"ALLOW_EXPIRED = False\n"},
        commit=True,
    )
    result_scenario.source("b", files={"b.py": b"B = True\n"}, commit=True)
    result_scenario.source("c", files={"c.py": b"C = True\n"}, commit=True)

    result_scenario.worker_context_versions = (1, 2)
    result_scenario.drive()

    assert result_scenario.adapter_start_count("integrate") == 0


def _input_hash(manifest: object) -> str:
    from hashlib import sha256

    from sidera_control_plane.domain.collaboration_runtime import canonical_json_bytes

    if isinstance(manifest, CodeInputManifestV1):
        mapping = manifest.to_mapping()
    else:
        assert isinstance(manifest, dict)
        mapping = manifest
    return sha256(
        canonical_json_bytes(
            {
                "schema_version": mapping["schema_version"],
                "workspace_id": mapping["workspace_id"],
                "root_task_id": mapping["root_task_id"],
                "request_id": mapping["request_id"],
                "execution_id": mapping["execution_id"],
                "selection_id": mapping["selection_id"],
                "selection_hash": mapping["selection_hash"],
                "repository_ref": mapping["repository_ref"],
                "base_commit": mapping["base_commit"],
                "sources": mapping["sources"],
            }
        )
    ).hexdigest()
