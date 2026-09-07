from __future__ import annotations

from copy import deepcopy


def integration_mapping() -> dict[str, object]:
    return {
        "schema_version": 1,
        "repository_ref": "repo://integration-test",
        "base_commit": "a" * 40,
        "integration_worker_item_key": "integrate",
        "input_group_keys": ["implementation"],
        "criteria": [
            {
                "key": "login",
                "text": "Expired credentials are rejected",
                "mode": "automatic",
                "check_keys": ["unit"],
            }
        ],
        "checks": [
            {
                "key": "unit",
                "version": 1,
                "argv": ["python3", "-m", "unittest"],
                "cwd": ".",
                "environment_ref": "env://python-test",
                "prepare_argvs": [],
                "timeout_seconds": 60,
                "success_exit_codes": [0],
                "report_paths": [],
                "required": True,
            }
        ],
        "verification_binding": {
            "adapter_module_ref": "00000000-0000-0000-0000-000000000010",
            "adapter_config_version": 1,
            "workspace_module_ref": "00000000-0000-0000-0000-000000000011",
            "workspace_config_version": 1,
        },
        "rework_policy": {
            "max_auto_rework_rounds": 2,
            "max_auto_rework_seconds": 1800,
            "allowed_worker_item_keys": ["login", "integrate"],
        },
        "apply_policy": {
            "apply_on_accept": False,
            "mode": "fast_forward_only",
            "target_ref": None,
        },
        "finalization_policy": "stop_unused",
    }


def independent_mapping() -> dict[str, object]:
    """Return a fresh mapping so tests cannot mutate shared fixture state."""

    return deepcopy(integration_mapping())
