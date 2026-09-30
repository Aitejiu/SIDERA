from pathlib import Path

import pytest

from sidera_control_plane.demo.environment import child_environment
from sidera_control_plane.demo.manifest import validate_manifest
from sidera_control_plane.demo.safety import (
    assert_demo_endpoint,
    assert_safe_child_environment,
    audit_demo_configuration,
    audit_demo_environment,
)
from tests.unit.interview_demo_support import manifest_for


def test_provider_keys_and_proxy_are_not_forwarded() -> None:
    environment = child_environment(
        {
            "PATH": "/usr/bin:/bin",
            "DEEPSEEK_API_KEY": "fake-test-secret",
            "OPENAI_API_KEY": "fake-test-secret",
            "HTTP_PROXY": "http://127.0.0.1:9",
        },
        {"SIDERA_INTERVIEW_DEMO_MANIFEST": "/private/tmp/demo/manifest.json"},
    )

    assert audit_demo_environment(environment) == ()
    assert "DEEPSEEK_API_KEY" not in environment
    assert "HTTP_PROXY" not in environment


def test_safe_child_environment_rejects_a_secret_reintroduced_by_a_caller() -> None:
    with pytest.raises(ValueError, match="forbidden_environment:OPENAI_API_KEY"):
        assert_safe_child_environment({"OPENAI_API_KEY": "fake-test-secret"})


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1.evil:15432",
        "http://user:pass@127.0.0.1:15432",
        "http://127.0.0.1:15432/redirect",
        "http://127.0.0.1:19999",
        "https://127.0.0.1:15432",
    ],
)
def test_endpoint_allowlist_fails_closed(tmp_path: Path, url: str) -> None:
    manifest = manifest_for(tmp_path)
    with pytest.raises(ValueError):
        assert_demo_endpoint(url, manifest)


def test_configuration_audit_rejects_a_symlinked_run_root(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    manifest.run_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    unsafe = manifest.model_copy(update={"run_root": link})

    assert "manifest_invalid" in audit_demo_configuration(unsafe)


def test_restore_manifest_can_keep_runtime_metadata_outside_restored_payload(
    tmp_path: Path,
) -> None:
    manifest = manifest_for(tmp_path).model_copy(
        update={
            "run_root": tmp_path / "restored" / "payload",
            "repository": tmp_path / "restored" / "payload" / "repository",
            "runtime_root": tmp_path / "restored" / "runtime",
            "restore_mode": "read_only",
        }
    )

    validate_manifest(manifest, Path(__file__).resolve().parents[4])
