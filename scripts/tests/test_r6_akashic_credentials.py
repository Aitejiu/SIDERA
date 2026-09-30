from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AKASHIC_ROOT = ROOT / "akashic-agent"
sys.path.insert(0, str(AKASHIC_ROOT))

from agent.model_runtime.auth.store import Credential, CredentialStore
from agent.model_runtime.store import ModelRegistryStore

from scripts.r6_akashic_credentials import sync_main_agent_credential


def _workspace_with_opencode_go(workspace: Path) -> None:
    store = ModelRegistryStore.for_workspace(workspace)
    store.replace_from_llm_config(
        {
            "main": "opencode_go_main",
            "runtimes": {
                "opencode_go_main": {
                    "provider": "opencode-go",
                    "model": "deepseek-v4.1-flash",
                    "auth": "main_credential",
                    "base_url": "https://opencode.ai/zen/go/v1",
                },
                "unrelated": {
                    "provider": "example",
                    "model": "example-model",
                    "auth": "unrelated_credential",
                    "base_url": "https://example.test/v1",
                },
            },
        },
        credentials={
            "main_credential": Credential(
                driver="api_key",
                access_token="stale-key",
            ),
            "unrelated_credential": Credential(
                driver="api_key",
                access_token="keep-this-key",
            ),
        },
    )


def _manifest() -> dict[str, object]:
    return {
        "profiles": [
            {
                "role": "main_agent",
                "provider": "opencode-go",
                "model_id": "opencode-go/deepseek-v4.1-flash",
                "endpoint_host": "opencode.ai",
                "credential_ref": "env:SIDERA_OPENCODE_GO_API_KEY",
            }
        ]
    }


def test_syncs_manifest_env_credential_to_matching_workspace_runtime(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _workspace_with_opencode_go(workspace)

    changed = sync_main_agent_credential(
        manifest=_manifest(),
        environment={"SIDERA_OPENCODE_GO_API_KEY": "current-key"},
        workspace=workspace,
    )

    credentials = CredentialStore.for_workspace(workspace)
    assert changed is True
    assert credentials.api_key("main_credential") == "current-key"
    assert credentials.api_key("unrelated_credential") == "keep-this-key"


def test_seeds_empty_acceptance_workspace_from_manifest_profile(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    changed = sync_main_agent_credential(
        manifest=_manifest(),
        environment={"SIDERA_OPENCODE_GO_API_KEY": "current-key"},
        workspace=workspace,
    )

    snapshot = ModelRegistryStore.for_workspace(workspace).read_snapshot()
    assert changed is True
    assert snapshot is not None
    assert len(snapshot.runtimes) == 1
    runtime = next(iter(snapshot.runtimes.values()))
    assert runtime.provider == "opencode-go"
    assert runtime.model == "deepseek-v4.1-flash"
    assert runtime.base_url == "https://opencode.ai/zen/go/v1"
    assert (
        CredentialStore.for_workspace(workspace).api_key(runtime.auth_id)
        == "current-key"
    )


def test_does_not_overwrite_when_manifest_credential_is_not_environment_backed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _workspace_with_opencode_go(workspace)
    manifest = _manifest()
    manifest["profiles"] = [
        {
            **manifest["profiles"][0],  # type: ignore[index]
            "credential_ref": "store:opencode-go",
        }
    ]

    changed = sync_main_agent_credential(
        manifest=manifest,
        environment={"SIDERA_OPENCODE_GO_API_KEY": "current-key"},
        workspace=workspace,
    )

    assert changed is False
    assert (
        CredentialStore.for_workspace(workspace).api_key("main_credential")
        == "stale-key"
    )


def test_rejects_ambiguous_matching_runtime_without_mutating_credentials(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ModelRegistryStore.for_workspace(workspace)
    store.replace_from_llm_config(
        {
            "main": "first",
            "runtimes": {
                runtime_id: {
                    "provider": "opencode-go",
                    "model": "deepseek-v4.1-flash",
                    "auth": auth_id,
                    "source_id": runtime_id,
                    "base_url": "https://opencode.ai/zen/go/v1",
                }
                for runtime_id, auth_id in (
                    ("first", "first_credential"),
                    ("second", "second_credential"),
                )
            },
        },
        credentials={
            "first_credential": Credential(
                driver="api_key",
                access_token="first-old-key",
            ),
            "second_credential": Credential(
                driver="api_key",
                access_token="second-old-key",
            ),
        },
    )

    with pytest.raises(ValueError, match="matching_runtime_ambiguous"):
        sync_main_agent_credential(
            manifest=_manifest(),
            environment={"SIDERA_OPENCODE_GO_API_KEY": "current-key"},
            workspace=workspace,
        )

    credentials = CredentialStore.for_workspace(workspace)
    assert credentials.api_key("first_credential") == "first-old-key"
    assert credentials.api_key("second_credential") == "second-old-key"
