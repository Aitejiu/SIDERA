"""Bind an isolated Akashic acceptance workspace to its declared env credential."""

from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

AKASHIC_ROOT = Path(__file__).resolve().parents[1] / "akashic-agent"
sys.path.insert(0, str(AKASHIC_ROOT))

from agent.model_runtime.auth.store import Credential, CredentialStore
from agent.model_runtime.errors import AuthenticationError
from agent.model_runtime.provider_profiles import get_provider_profile
from agent.model_runtime.store import ModelRegistryStore

_ENV_REFERENCE = re.compile(r"^env:([A-Z][A-Z0-9_]*)$")


def sync_main_agent_credential(
    *,
    manifest: Mapping[str, object],
    environment: Mapping[str, str],
    workspace: Path,
) -> bool:
    """Copy a declared main-agent env credential into the run-owned model store."""

    profiles = manifest.get("profiles")
    if not isinstance(profiles, list):
        raise TypeError("profiles_invalid")
    main_profiles = [
        item
        for item in profiles
        if isinstance(item, Mapping) and item.get("role") == "main_agent"
    ]
    if len(main_profiles) != 1:
        raise ValueError("main_agent_profile_invalid")
    profile = main_profiles[0]
    if str(profile.get("provider") or "").strip().lower() != "opencode-go":
        return False

    credential_ref = profile.get("credential_ref")
    if not isinstance(credential_ref, str) or not credential_ref.startswith("env:"):
        return False
    reference = _ENV_REFERENCE.fullmatch(credential_ref)
    if reference is None:
        raise ValueError("main_agent_credential_ref_invalid")
    raw_api_key = environment.get(reference.group(1), "")
    if not isinstance(raw_api_key, str) or not raw_api_key.strip():
        raise ValueError("main_agent_credential_missing")
    api_key = raw_api_key.strip()

    model_id = str(profile.get("model_id") or "").strip()
    model_prefix = "opencode-go/"
    model = model_id.removeprefix(model_prefix)
    if not model or model == model_id and model_id.startswith("opencode-go/"):
        raise ValueError("main_agent_model_invalid")
    endpoint_host = str(profile.get("endpoint_host") or "").strip().lower()
    if not endpoint_host:
        raise ValueError("main_agent_endpoint_invalid")

    model_store = ModelRegistryStore.for_workspace(workspace)
    snapshot = model_store.read_snapshot()
    if snapshot is None:
        provider_profile = get_provider_profile("opencode-go")
        if provider_profile is None:
            raise ValueError("main_agent_provider_profile_missing")
        base_url = provider_profile.default_base_url
        if _endpoint_host(base_url) != endpoint_host:
            raise ValueError("main_agent_endpoint_mismatch")
        runtime_id = "acceptance_opencode_go_main"
        auth_id = "acceptance_opencode_go_main"
        model_store.replace_from_llm_config(
            {
                "main": runtime_id,
                "runtimes": {
                    runtime_id: {
                        "provider": "opencode-go",
                        "model": model,
                        "source_id": runtime_id,
                        "source_name": "OpenCode Go acceptance",
                        "auth": auth_id,
                        "base_url": base_url,
                    }
                },
            },
            credentials={
                auth_id: Credential(
                    driver="api_key",
                    access_token=api_key,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            },
        )
        if CredentialStore.for_workspace(workspace).api_key(auth_id) != api_key:
            raise RuntimeError("main_agent_credential_seed_verification_failed")
        return True
    matches = [
        runtime
        for runtime in snapshot.runtimes.values()
        if runtime.provider.strip().lower() == "opencode-go"
        and runtime.model.strip() == model
        and _endpoint_host(runtime.base_url) == endpoint_host
    ]
    if not matches or any(
        not runtime.source_id or not runtime.auth_id for runtime in matches
    ):
        raise ValueError("main_agent_matching_runtime_ambiguous")
    connections = {(runtime.source_id, runtime.auth_id) for runtime in matches}
    if len(connections) != 1:
        raise ValueError("main_agent_matching_runtime_ambiguous")
    auth_id = next(iter(connections))[1]

    credential_store = CredentialStore.for_workspace(workspace)
    try:
        current = credential_store.get(auth_id)
    except AuthenticationError:
        current = None
    if (
        current is not None
        and current.driver == "api_key"
        and current.access_token == api_key
    ):
        return False

    credential_store.put(
        auth_id,
        Credential(
            driver="api_key",
            access_token=api_key,
            updated_at=datetime.now(timezone.utc).isoformat(),
        ),
    )
    if credential_store.api_key(auth_id) != api_key:
        raise RuntimeError("main_agent_credential_sync_verification_failed")
    return True


def _endpoint_host(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    return f"{host}:{parsed.port}" if parsed.port else host


def _load_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        raise ValueError("private_input_invalid")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_private_json(path: Path) -> object:
    value = _load_json(path)
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("private_input_permissions_invalid")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--environment-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()

    try:
        manifest = _load_json(args.manifest)
        environment = _load_private_json(args.environment_file)
        if not isinstance(manifest, Mapping) or not isinstance(environment, Mapping):
            raise TypeError("private_input_invalid")
        changed = sync_main_agent_credential(
            manifest=manifest,
            environment=environment,
            workspace=args.workspace,
        )
    except (
        AuthenticationError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(
            f"main-agent credential sync failed: {type(exc).__name__}", file=sys.stderr
        )
        return 1

    print(
        "main-agent credential synced" if changed else "main-agent credential unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
