import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from time import monotonic, sleep, time_ns
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4, uuid5

SOURCE_PATHS = (
    "apps/server/tests/support/native_product_harness.py",
    "apps/server/tests/support/native_product_flow.py",
    "apps/server/tests/unit/test_native_product_harness.py",
    "apps/server/tests/unit/test_native_product_flow.py",
    "apps/server/tests/integration/test_product_native_flow.py",
    "apps/web/playwright.native.config.ts",
    "apps/web/tests/e2e/native-harness-fixtures.ts",
    "apps/web/tests/e2e/native-harness.spec.ts",
    "apps/web/tests/e2e/native-harness-resume.spec.ts",
    "scripts/native-harness-check.py",
    "scripts/tests/test_native_harness_check.py",
    "apps/server/tests/support/agent_management_desktop.py",
    "apps/server/tests/unit/test_agent_management_targets.py",
    "apps/server/tests/support/controlled_knowledge.py",
    "apps/server/tests/unit/test_external_knowledge_fixture.py",
    "apps/server/tests/integration/conftest.py",
    "apps/server/src/sidera_product/agents/routes.py",
    "apps/server/src/sidera_product/agents/schemas.py",
    "apps/web/src/features/agents/api.ts",
    "apps/web/src/features/agents/ResourceEditor.tsx",
    "services/control-plane/src/sidera_control_plane/application/event_ingestor.py",
    "services/control-plane/src/sidera_control_plane/application/worker_gateway.py",
    "services/control-plane/src/sidera_control_plane/application/execution_code_inputs.py",
    "services/control-plane/src/sidera_control_plane/application/activation_reconciler.py",
    "services/control-plane/src/sidera_control_plane/application/context_requests.py",
    "services/control-plane/src/sidera_control_plane/application/handoff_context.py",
)
STAGES = (
    "configuration",
    "conversation",
    "goal_submitted",
    "proposal_ready",
    "plan_confirmed",
    "dependency_execution",
    "worker_input",
    "result_verified",
    "result_accepted",
    "navigation_history",
    "owned_cleanup",
)


class ManifestError(ValueError):
    pass


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.resolve() != path:
        raise ManifestError("private_path_refused")
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
    ):
        raise ManifestError("private_directory_refused")
    return path


def private_read(path):
    try:
        path = Path(path)
        private_directory(path.parent)
        if path.resolve() != path:
            raise ManifestError("private_path_refused")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 2097152
            ):
                raise ManifestError("private_file_refused")
            return json.load(stream)
    except (OSError, ValueError, TypeError):
        raise ManifestError("private_file_refused") from None


def private_write(path, value):
    private_directory(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def cleanup_reference(manifest):
    return {
        "schema_version": 1,
        "owner_uid": manifest["owner_uid"],
        "allocation_id": manifest["allocation_id"],
        "workspace_id": manifest["workspace_id"],
        "namespace": manifest["namespace"],
        "config_hash": manifest["config_hash"],
        "owner_pid": manifest["process_identities"]["owner_pid"],
        "operation_id": str(uuid5(UUID(manifest["namespace"]), "owned_cleanup")),
    }


def matches_cleanup_reference(value, reference, *, exact=False):
    return (
        type(value) is dict
        and (not exact or set(value) == set(reference))
        and all(
            key in value
            and type(value[key]) is type(expected)
            and value[key] == expected
            for key, expected in reference.items()
        )
    )


def _loopback(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not parsed.port
    ):
        raise ManifestError("native_origin_refused")


def read_manifest(path, *, allow_ui_change=False):
    try:
        path = Path(path)
        if not path.is_absolute():
            raise ManifestError("manifest_path_required")
        value = private_read(path)
        fields = {
            "schema_version",
            "owner_uid",
            "allocation_id",
            "workspace_id",
            "namespace",
            "principal_id",
            "repository_directory",
            "repository_head",
            "source_root",
            "source_pins",
            "config_path",
            "config_hash",
            "mode",
            "controlled_bridge",
            "base_url",
            "backend_url",
            "controller_url",
            "transport_path",
            "checkpoint_path",
            "cleanup_path",
            "owner_receipt_path",
            "process_identities",
            "target_facts",
            "guard_custody",
        }
        if (
            type(value) is not dict
            or set(value) != fields
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["owner_uid"] != os.getuid()
        ):
            raise ManifestError("manifest_identity_refused")
        if not re.fullmatch(
            r"sidera-native-harness:product:n17:[0-9]{8}:[a-z0-9]+",
            value["allocation_id"],
        ):
            raise ManifestError("manifest_allocation_refused")
        UUID(value["workspace_id"])
        UUID(value["namespace"])
        if (
            value["mode"] not in {"fake", "real"}
            or type(value["controlled_bridge"]) is not bool
            or value["controlled_bridge"] != (value["mode"] == "fake")
            or value["guard_custody"] not in {"lab", "caller"}
        ):
            raise ManifestError("manifest_mode_refused")
        for key in ("base_url", "backend_url"):
            _loopback(value[key])
        if value["mode"] == "fake":
            _loopback(value["controller_url"])
        elif value["controller_url"] is not None:
            raise ManifestError("real_control_bridge_refused")
        for key in (
            "config_path",
            "transport_path",
            "owner_receipt_path",
            "checkpoint_path",
            "cleanup_path",
            "repository_directory",
        ):
            selected = Path(value[key])
            if (
                not selected.is_absolute()
                or selected.parent != path.parent
                or selected.resolve() != selected
            ):
                raise ManifestError("manifest_owned_path_refused")
        for key in ("checkpoint_path", "cleanup_path", "repository_directory"):
            private_directory(value[key])
        config = private_read(value["config_path"])
        if (
            fingerprint(config) != value["config_hash"]
            or config["settings"]["workspace_id"] != value["workspace_id"]
        ):
            raise ManifestError("manifest_config_refused")
        owner = private_read(value["owner_receipt_path"])
        if not matches_cleanup_reference(owner, cleanup_reference(value)):
            raise ManifestError("manifest_owner_refused")
        root = Path(value["source_root"])
        if (
            not root.is_absolute()
            or root.resolve() != root
            or not root.is_dir()
            or set(value["source_pins"]) != set(SOURCE_PATHS)
        ):
            raise ManifestError("manifest_source_refused")
        changes = []
        for name, pin in value["source_pins"].items():
            source = root / name
            expected_scope = "ui" if name.startswith("apps/web/") else "producer"
            if (
                set(pin) != {"sha256", "scope"}
                or pin["scope"] != expected_scope
                or not re.fullmatch(r"[0-9a-f]{64}", pin["sha256"])
                or source.resolve() != source
                or not source.is_file()
            ):
                raise ManifestError("manifest_source_refused")
            if hashlib.sha256(source.read_bytes()).hexdigest() != pin["sha256"]:
                if not allow_ui_change or expected_scope != "ui":
                    raise ManifestError("manifest_source_changed")
                changes.append(name)
        value["_source_changes"] = changes
        return value
    except (OSError, KeyError, ValueError, TypeError):
        raise ManifestError("manifest_refused") from None


def _safe_record(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if any(
                part in key.lower()
                for part in (
                    "token",
                    "cookie",
                    "authorization",
                    "bootstrap",
                    "native_handle",
                    "password",
                    "secret",
                )
            ):
                raise ManifestError("checkpoint_secret_refused")
            _safe_record(item)
    elif isinstance(value, list):
        for item in value:
            _safe_record(item)


class Journal:
    def __init__(self, manifest):
        self.manifest = manifest

    def stage_fingerprint(self, step):
        pins = {
            name: pin["sha256"]
            for name, pin in self.manifest["source_pins"].items()
            if pin["scope"] == "producer"
        }
        if step == "navigation_history":
            pins.update(
                {
                    name: hashlib.sha256(
                        (Path(self.manifest["source_root"]) / name).read_bytes()
                    ).hexdigest()
                    for name, pin in self.manifest["source_pins"].items()
                    if pin["scope"] == "ui"
                }
            )
        return fingerprint({"config_hash": self.manifest["config_hash"], "pins": pins})

    def append(
        self,
        step,
        status,
        *,
        original_ids=None,
        observed=None,
        versions=None,
        step_id=None,
    ):
        if step not in STAGES or status not in {
            "pending",
            "passed",
            "failed",
            "incomplete",
            "invalidated",
        }:
            raise ManifestError("checkpoint_status_refused")
        record = {
            "schema_version": 1,
            "owner_uid": os.getuid(),
            "workspace_id": self.manifest["workspace_id"],
            "allocation_id": self.manifest["allocation_id"],
            "config_hash": self.manifest["config_hash"],
            "step": step,
            "step_id": step_id or str(uuid4()),
            "status": status,
            "original_ids": original_ids or {},
            "versions": versions or {},
            "fingerprint": self.stage_fingerprint(step),
            "observed": observed or {},
            "time": datetime.now(UTC).isoformat(),
            "sequence": time_ns() // 1000,
        }
        _safe_record(record)
        path = (
            Path(self.manifest["checkpoint_path"])
            / f"{record['sequence']}-{uuid4().hex}.json"
        )
        private_write(path, record)
        return record

    def records(self):
        records = []
        for path in private_directory(self.manifest["checkpoint_path"]).iterdir():
            if path.suffix != ".json":
                raise ManifestError("checkpoint_file_refused")
            record = private_read(path)
            if (
                record["schema_version"] != 1
                or record["owner_uid"] != os.getuid()
                or record["workspace_id"] != self.manifest["workspace_id"]
                or record["allocation_id"] != self.manifest["allocation_id"]
                or record["config_hash"] != self.manifest["config_hash"]
                or record["step"] not in STAGES
                or record["status"]
                not in {"pending", "passed", "failed", "incomplete", "invalidated"}
            ):
                raise ManifestError("checkpoint_identity_refused")
            UUID(record["step_id"])
            _safe_record(record)
            records.append(record)
        return sorted(records, key=lambda record: record["sequence"])


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OSError("native_redirect_refused")


class PublicClient:
    def __init__(self, manifest):
        self.manifest = manifest
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def get(self, path):
        if not path.startswith("/") or path.startswith("//"):
            raise ManifestError("public_read_refused")
        headers = {"Origin": self.manifest["base_url"]}
        session_path = Path(self.manifest["transport_path"]).parent / "session.json"
        if session_path.exists():
            state = private_read(session_path)
            host = urlsplit(self.manifest["base_url"]).hostname
            cookies = [
                item
                for item in state["cookies"]
                if item["name"] in {"sidera_local_session", "sidera_csrf"}
                and item["domain"].strip("[]") == host
            ]
            headers["Cookie"] = "; ".join(
                item["name"] + "=" + item["value"] for item in cookies
            )
        request = Request(
            self.manifest["backend_url"] + path, headers=headers, method="GET"
        )
        with self.opener.open(request, timeout=10) as response:
            return json.load(response)


class NativeCheck:
    def __init__(self, manifest, *, client=None, runner=None):
        self.manifest = manifest
        self.client = client or PublicClient(manifest)
        self.runner = runner or self._desktop_step
        self.journal = Journal(manifest)

    def _report(self, reason, *, functional=False, cleanup=False):
        return {
            "status": "PASS" if functional and cleanup else "INCOMPLETE",
            "reason": reason,
            "workspace_id": self.manifest["workspace_id"],
            "functional": functional,
            "cleanup": cleanup,
        }

    def preflight(self):
        if self.manifest["mode"] != "fake":
            return self._report("provider_mode_not_authorized")
        try:
            if (
                self.client.get("/healthz")["status"] != "ok"
                or self.client.get("/readyz")["ready"] is not True
            ):
                return self._report("product_not_ready")
            session = self.client.get("/product/v1/session")
            if (
                session["workspace_id"] != self.manifest["workspace_id"]
                or session["principal_id"] != self.manifest["principal_id"]
            ):
                return self._report("current_human_required")
            prefix = f"/product/v1/workspaces/{self.manifest['workspace_id']}"
            defaults = self.client.get(prefix + "/defaults")
            if (
                defaults["workspace_id"] != self.manifest["workspace_id"]
                or not defaults["main_profile_id"]
                or len(
                    {
                        defaults[key]
                        for key in (
                            "main_profile_id",
                            "planner_profile_id",
                            "worker_profile_id",
                        )
                    }
                )
                != 1
            ):
                return self._report("profile_defaults_required")
            setup = self.client.get(prefix + "/setup")
            if not self._setup_matches(setup, defaults["planner_profile_id"]):
                return self._report("workspace_setup_required")
            saved = next(
                (
                    record["original_ids"]["profile_ref"]
                    for record in reversed(self.journal.records())
                    if record["original_ids"].get("profile_ref")
                ),
                None,
            )
            if saved is not None and setup["planner_agent_ref"] != saved:
                return self._report("workspace_setup_required")
            return {**self._report("preflight_ready"), "status": "READY"}
        except (OSError, ValueError, KeyError, TypeError):
            return self._report("preflight_read_unavailable")

    def _profile_matches(self, ref, expected=None):
        return (
            isinstance(ref, dict)
            and ref.get("workspace_id") == self.manifest["workspace_id"]
            and isinstance(ref.get("profile_id"), str)
            and bool(UUID(ref["profile_id"]))
            and type(ref.get("revision")) is int
            and ref["revision"] >= 1
            and (expected is None or ref == expected)
        )

    def _setup_matches(self, setup, profile_id):
        if not isinstance(setup, dict):
            return False
        policy, planner = (
            setup.get("planning_policy_ref"),
            setup.get("planner_agent_ref"),
        )
        return (
            setup.get("workspace_id") == self.manifest["workspace_id"]
            and type(setup.get("version")) is int
            and setup["version"] >= 1
            and isinstance(setup.get("repository_ref"), str)
            and bool(setup["repository_ref"].strip())
            and isinstance(policy, dict)
            and isinstance(policy.get("policy_id"), str)
            and bool(UUID(policy["policy_id"]))
            and type(policy.get("version")) is int
            and policy["version"] >= 1
            and self._profile_matches(planner)
            and planner["profile_id"] == profile_id
        )

    def _configuration_matches(self, ids, *, target=False):
        prefix = "target_" if target else ""
        workspace = self.manifest["workspace_id"]
        try:
            created, profile, resource = (
                ids[prefix + "profile_create_ref"],
                ids[prefix + "profile_ref"],
                ids["task_tool_resource_ref"],
            )
            connection_id = ids["task_tool_connection_id"]
            if (
                not self._profile_matches(created)
                or created["revision"] != 1
                or not self._profile_matches(profile)
                or profile != {**created, "revision": 2}
                or profile["profile_id"] != ids[prefix + "profile_id"]
                or ids[prefix + "configuration_expected_ref"] != created
                or ids[prefix + "configuration_resource_ref"] != resource
                or not isinstance(resource, dict)
                or set(resource) != {"workspace_id", "resource_id", "revision"}
                or resource["workspace_id"] != workspace
                or type(resource["revision"]) is not int
                or resource["revision"] != 1
                or not isinstance(resource["resource_id"], str)
                or not UUID(resource["resource_id"])
                or not isinstance(connection_id, str)
                or not UUID(connection_id)
                or (target and profile["profile_id"] == ids["profile_id"])
            ):
                return False
            originals = (
                (ids[prefix + "profile_create_operation_id"], created),
                (ids["task_tool_resource_operation_id"], resource),
                (ids[prefix + "configuration_operation_id"], profile),
            )
            if (
                any(
                    not isinstance(operation, str) or not operation
                    for operation, _ in originals
                )
                or len({operation for operation, _ in originals}) != 3
            ):
                return False
            for operation, ref in originals:
                configured = self.client.get(
                    f"/product/v1/workspaces/{workspace}/configuration-requests?"
                    + urlencode({"operationId": operation})
                )
                if (
                    configured["operationId"] != operation
                    or configured["state"] not in {"succeeded", "reconciled"}
                    or configured["target_ref"] != ref
                    or configured["result_ref"] != ref
                ):
                    return False
            saved = self.client.get(
                f"/agents/v1/workspaces/{workspace}/profiles/{profile['profile_id']}?revision={profile['revision']}"
            )
            tool = self.client.get(
                f"/agents/v1/workspaces/{workspace}/resources/{resource['resource_id']}?revision={resource['revision']}"
            )
            metadata = self.client.get(
                f"/product/v1/workspaces/{workspace}/tool-connections"
            )
            return (
                saved["profile"]["ref"] == profile
                and saved["status"]["profile_id"] == profile["profile_id"]
                and saved["status"]["enabled"] is True
                and saved["profile"]["draft"]["runtime_ref"]
                == {"workspace_id": workspace, "runtime_id": ids["runtime_id"]}
                and saved["profile"]["draft"]["resource_refs"] == [resource]
                and tool["resource"]["ref"] == resource
                and tool["status"]["resource_id"] == resource["resource_id"]
                and tool["status"]["enabled"] is True
                and tool["resource"]["draft"]["kind"] == "mcp"
                and tool["resource"]["draft"]["settings"]
                == {"connection_id": connection_id}
                and tool["resource"]["draft"]["secret_refs"] == []
                and metadata
                == [
                    {
                        "workspace_id": workspace,
                        "connection_id": connection_id,
                        "name": "SIDERA Task Tools",
                        "availability": "configured",
                    }
                ]
            )
        except (ValueError, KeyError, TypeError):
            return False

    def _observe(self, record):
        ids, step = record["original_ids"], record["step"]
        required = {
            "configuration": (
                "configuration_operation_id",
                "profile_create_operation_id",
                "profile_create_ref",
                "task_tool_resource_operation_id",
                "task_tool_resource_ref",
                "task_tool_connection_id",
                "runtime_id",
                "profile_id",
                "profile_ref",
            ),
            "conversation": ("thread_id", "profile_ref"),
            "goal_submitted": (
                "thread_id",
                "turn_id",
                "turn_operation_id",
                "root_task_id",
                "planning_request_id",
            ),
            "proposal_ready": (
                "root_task_id",
                "planning_request_id",
                "proposal_id",
                "proposal_revision",
                "proposal_content_hash",
            ),
            "plan_confirmed": (
                "confirmation_command_id",
                "materialization_id",
                "plan_snapshot_id",
                "plan_generation",
                "source_worker_item_id",
            ),
            "dependency_execution": (
                "source_worker_item_id",
                "source_execution_id",
                "source_run_id",
                "profile_ref",
            ),
            "worker_input": (
                "input_command_id",
                "input_id",
                "source_worker_item_id",
                "source_execution_id",
                "source_run_id",
            ),
            "result_verified": (
                "root_task_id",
                "candidate_id",
                "candidate_hash",
                "verification_id",
                "result_commit",
                "result_tree",
            ),
            "result_accepted": (
                "root_task_id",
                "candidate_id",
                "candidate_hash",
                "acceptance_command_id",
            ),
            "navigation_history": (
                "root_task_id",
                "candidate_id",
                "candidate_hash",
                "acceptance_command_id",
                "source_run_id",
                "source_execution_id",
                "source_worker_item_id",
            ),
        }
        if not ids or any(not ids.get(key) for key in required[step]):
            return False
        transition = (
            private_read(self.manifest["config_path"]).get("scenario", "main")
            == "transition"
        )
        focused = step == "dependency_execution" and transition
        if focused and any(
            not ids.get(key)
            for key in (
                "root_task_id",
                "source_session_id",
                "source_context_version",
                "binding_version",
                "plan_generation",
                "target_profile_id",
                "target_profile_ref",
                "target_configuration_operation_id",
                "handoff_prepare_command_id",
                "handoff_confirm_command_id",
                "handoff_proposal_id",
                "handoff_transition_id",
            )
        ):
            return False
        for key, selected in ids.items():
            if key.endswith("_id") and key not in {
                "configuration_operation_id",
                "profile_create_operation_id",
                "task_tool_resource_operation_id",
                "target_profile_create_operation_id",
                "target_configuration_operation_id",
                "turn_operation_id",
            }:
                UUID(selected)
        prefix = f"/api/v1/workspaces/{self.manifest['workspace_id']}/"
        profile = ids.get("profile_ref")
        if profile is not None and not self._profile_matches(profile):
            return False
        if ids.get("configuration_operation_id") and not self._configuration_matches(
            ids
        ):
            return False
        if (
            transition
            and step in {"configuration", "dependency_execution"}
            and not self._configuration_matches(ids, target=True)
        ):
            return False
        if step == "configuration":
            runtime = self.client.get(
                f"/agents/v1/workspaces/{self.manifest['workspace_id']}/runtimes/{ids['runtime_id']}"
            )
            if (
                runtime["ref"]["workspace_id"] != self.manifest["workspace_id"]
                or runtime["ref"]["runtime_id"] != ids["runtime_id"]
                or runtime["enabled"] is not True
                or runtime["installed"] is not True
                or self.preflight()["status"] != "READY"
            ):
                return False
        for key, selected in ids.items():
            if key == "command_id" or key.endswith("_command_id"):
                UUID(selected)
                item = self.client.get(prefix + "commands/" + selected)
                if (
                    item["command_id"] != selected
                    or item["status"] != "applied"
                    or item.get("result") is None
                ):
                    return False
        result = None
        if ids.get("root_task_id"):
            result = self.client.get(prefix + f"tasks/{ids['root_task_id']}/results")
            if (
                result["workspace_id"] != self.manifest["workspace_id"]
                or result["root_task_id"] != ids["root_task_id"]
            ):
                return False
        if ids.get("planning_request_id"):
            planning = self.client.get(
                prefix + "planning-requests/" + ids["planning_request_id"]
            )
            if (
                planning["workspace_id"] != self.manifest["workspace_id"]
                or planning["id"] != ids["planning_request_id"]
            ):
                return False
            if ids.get("proposal_revision") and (
                planning["current_proposal_revision"] != ids["proposal_revision"]
                or planning["current_proposal_content_hash"]
                != ids["proposal_content_hash"]
            ):
                return False
            if step == "proposal_ready" and planning["status"] not in {
                "proposal_ready",
                "materialized",
            }:
                return False
            if ids.get("materialization_id"):
                materialized = self.client.get(
                    prefix
                    + f"planning-requests/{ids['planning_request_id']}/materialization"
                )
                for field in (
                    "id",
                    "workspace_id",
                    "planning_request_id",
                    "root_task_id",
                    "proposal_revision_id",
                    "plan_snapshot_id",
                    "plan_generation",
                ):
                    selected = ids.get(
                        {
                            "id": "materialization_id",
                            "proposal_revision_id": "proposal_id",
                        }.get(field, field),
                        self.manifest.get(field),
                    )
                    if selected is not None and materialized[field] != selected:
                        return False
                if (
                    ids.get("source_worker_item_id")
                    and materialized["worker_item_mapping"].get("source")
                    != ids["source_worker_item_id"]
                ):
                    return False
        item_id = ids.get("source_worker_item_id", ids.get("worker_item_id"))
        if item_id:
            context = self.client.get(prefix + f"worker-items/{item_id}/native-context")
            if (
                context["workspace_id"] != self.manifest["workspace_id"]
                or context["worker_item_id"] != item_id
            ):
                return False
            run = context.get("run")
            original_run = ids.get("source_run_id", ids.get("run_id"))
            execution = ids.get("source_execution_id", ids.get("execution_id"))
            current = context.get("execution")
            address = context.get("context")
            if original_run and (
                run is None
                or current is None
                or current["run_id"] != original_run
                or current["execution_id"] != execution
            ):
                if not execution:
                    return False
                trace = self.client.get(
                    prefix
                    + f"worker-items/{item_id}/executions/{execution}/native-trace"
                )
                if any(
                    trace[field] != selected
                    for field, selected in (
                        ("workspace_id", self.manifest["workspace_id"]),
                        ("worker_item_id", item_id),
                        ("execution_id", execution),
                        ("run_id", original_run),
                    )
                ) or not self._profile_matches(trace["profile_ref"], profile):
                    return False
            elif original_run:
                if (
                    current["workspace_id"] != self.manifest["workspace_id"]
                    or current["worker_item_id"] != item_id
                    or not self._profile_matches(run["profile_ref"], profile)
                ):
                    return False
                if address is not None:
                    if (
                        address["worker_item_id"] != item_id
                        or address["execution_id"] != execution
                        or address["run_id"] != original_run
                    ):
                        return False
                    for field in ("plan_generation", "binding_version"):
                        if ids.get(field) is not None and address[field] != ids[field]:
                            return False
        if step == "worker_input":
            query = {
                "workspace_id": self.manifest["workspace_id"],
                "execution_id": ids["source_execution_id"],
            }
            if ids.get("source_session_id"):
                query["session_id"] = ids["source_session_id"]
            rows = self.client.get("/api/v1/gateway/inputs?" + urlencode(query))[
                "items"
            ]
            if not any(
                item["id"] == ids["input_id"]
                and item["execution_id"] == ids["source_execution_id"]
                and item["status"] == "delivered"
                for item in rows
            ):
                return False
        if ids.get("candidate_id"):
            candidate = None if result is None else result.get("current_candidate")
            if (
                candidate is None
                or candidate["candidate_id"] != ids["candidate_id"]
                or candidate["candidate_hash"] != ids["candidate_hash"]
            ):
                return False
            detail = self.client.get(
                prefix + f"tasks/{ids['root_task_id']}/results/{ids['candidate_id']}"
            )
            if (
                detail["workspace_id"] != self.manifest["workspace_id"]
                or detail["root_task_id"] != ids["root_task_id"]
                or detail["candidate"]["candidate_id"] != ids["candidate_id"]
                or detail["candidate"]["candidate_hash"] != ids["candidate_hash"]
            ):
                return False
            if ids.get("verification_id") and (
                candidate["result_commit"] != ids["result_commit"]
                or candidate["result_tree"] != ids["result_tree"]
                or not any(
                    check["verification_id"] == ids["verification_id"]
                    and check["candidate_id"] == ids["candidate_id"]
                    and check["check_key"] == "owned-check"
                    and check["status"] == "passed"
                    and type(check["exit_code"]) is int
                    and check["exit_code"] == 0
                    and check["actual_commit"] == ids["result_commit"]
                    and check["actual_tree"] == ids["result_tree"]
                    for check in detail["verifications"]
                )
            ):
                return False
        if step in {"result_accepted", "navigation_history"}:
            acceptance = None if result is None else result.get("acceptance")
            if (
                result is None
                or result["task_status"] != "succeeded"
                or acceptance is None
                or any(
                    acceptance[field] != ids[key]
                    for field, key in (
                        ("candidate_id", "candidate_id"),
                        ("candidate_hash", "candidate_hash"),
                        ("command_id", "acceptance_command_id"),
                    )
                )
            ):
                return False
        if ids.get("thread_id"):
            path = "/main-agent/v1/threads/" + ids["thread_id"]
            thread = self.client.get(path)
            if thread["id"] != ids["thread_id"] or not self._profile_matches(
                thread["profileRef"], profile
            ):
                return False
            if ids.get("turn_id"):
                path += "/turns/" + ids["turn_id"]
                turn = self.client.get(path)
                if (
                    turn["id"] != ids["turn_id"]
                    or turn["threadId"] != ids["thread_id"]
                    or turn["operationId"] != ids["turn_operation_id"]
                    or turn["operationState"] not in {"accepted", "delivered"}
                    or not self._profile_matches(turn["profileRef"], profile)
                ):
                    return False
                if ids.get("chat_run_id") and turn["runId"] != ids["chat_run_id"]:
                    return False
        if focused:
            target = ids["target_profile_ref"]
            if (
                not self._profile_matches(target)
                or target["profile_id"] != ids["target_profile_id"]
                or target["profile_id"] == profile["profile_id"]
            ):
                return False
            for command_key, aggregate_key in (
                ("handoff_prepare_command_id", "handoff_proposal_id"),
                ("handoff_confirm_command_id", "handoff_transition_id"),
            ):
                command = self.client.get(prefix + "commands/" + ids[command_key])
                if command["result"].get("aggregate_id") != ids[aggregate_key]:
                    return False
            proposal = self.client.get(
                prefix + "handoff-proposals/" + ids["handoff_proposal_id"]
            )
            transition = self.client.get(
                prefix + "handoff-transitions/" + ids["handoff_transition_id"]
            )
            if (
                proposal["id"] != ids["handoff_proposal_id"]
                or proposal["root_task_id"] != ids["root_task_id"]
                or proposal["proposal_schema_version"] != 2
                or transition["handoff_schema_version"] != 2
                or transition["id"] != ids["handoff_transition_id"]
                or transition["proposal_id"] != ids["handoff_proposal_id"]
                or transition["failure_code"] != "source_stop_unconfirmed"
                or transition["target_execution_id"] is not None
                or transition["target_run_id"] is not None
                or transition["available_recovery_strategies"] != []
            ):
                return False
            for document in (proposal, transition):
                if any(
                    document[field] != selected
                    for field, selected in (
                        ("workspace_id", self.manifest["workspace_id"]),
                        ("worker_item_id", ids["source_worker_item_id"]),
                        ("source_execution_id", ids["source_execution_id"]),
                        ("source_run_id", ids["source_run_id"]),
                        ("source_session_id", ids["source_session_id"]),
                        ("source_binding_version", ids["binding_version"]),
                    )
                ) or (
                    not self._profile_matches(document["source_profile_ref"], profile)
                    or not self._profile_matches(document["target_profile_ref"], target)
                    or document["target_assignee"]
                    != {"kind": "profile", "id": ids["target_profile_id"]}
                    or document["plan_base"]["generation"] != ids["plan_generation"]
                    or document["context"]["source_context_version"]
                    != ids["source_context_version"]
                ):
                    return False
        return True

    def run(self, *, resume=False):
        if self.manifest["mode"] != "fake":
            return self._report("provider_mode_not_authorized")
        try:
            config = private_read(self.manifest["config_path"])
            if config.get("scenario", "main") == "transition":
                return self._report("focused_case_requires_explicit_desktop_entry")
            records = self.journal.records()
            if resume and not records:
                return self._report("original_ids_required")
            latest = {record["step"]: record for record in records}
            ids = {}
            for record in records:
                ids.update(record["original_ids"])
            for step in STAGES:
                record = latest.get(step)
                if record is not None and step != "owned_cleanup":
                    if not self._observe(record):
                        return self._report("original_operation_incomplete")
                    if record["status"] == "passed" and record[
                        "fingerprint"
                    ] == self.journal.stage_fingerprint(step):
                        continue
                    if step != "navigation_history" and record["status"] == "passed":
                        self.journal.append(
                            step,
                            "invalidated",
                            original_ids=record["original_ids"],
                            observed={"reason": "business_source_changed"},
                        )
                        return self._report("business_source_changed")
                    if step == "navigation_history" and record[
                        "fingerprint"
                    ] != self.journal.stage_fingerprint(step):
                        self.journal.append(
                            step,
                            "invalidated",
                            original_ids=record["original_ids"],
                            observed={
                                "changed_sources": self.manifest.get(
                                    "_source_changes", []
                                )
                            },
                        )
                elif resume and records and step != "owned_cleanup" and not ids:
                    return self._report("original_ids_required")
                pending = self.journal.append(
                    step,
                    "pending",
                    original_ids=ids,
                    observed={"resume": record is not None},
                    step_id=None if record is None else record["step_id"],
                )
                try:
                    observed = self.runner(step, pending)
                except Exception:  # noqa: BLE001 - retain original failed stage without raw transport details
                    self.journal.append(
                        step,
                        "failed",
                        original_ids=ids,
                        observed={"reason": "stage_execution_failed"},
                        step_id=pending["step_id"],
                    )
                    return self._report(
                        "stage_execution_failed", functional=step == "owned_cleanup"
                    )
                if (
                    not isinstance(observed, dict)
                    or observed.get("canonical") is not True
                ):
                    self.journal.append(
                        step,
                        "incomplete",
                        original_ids=ids,
                        observed={"reason": "canonical_proof_required"},
                        step_id=pending["step_id"],
                    )
                    return self._report(
                        "canonical_proof_required", functional=step == "owned_cleanup"
                    )
                ids.update(observed.get("original_ids", {}))
                self.journal.append(
                    step,
                    "passed",
                    original_ids=ids,
                    observed={
                        key: value
                        for key, value in observed.items()
                        if key != "original_ids"
                    },
                    step_id=pending["step_id"],
                )
            return self._report("owned_journey_complete", functional=True, cleanup=True)
        except (OSError, ValueError, KeyError, TypeError):
            return self._report("original_read_or_journal_unavailable")

    def cleanup(self, *, wait_seconds=45):
        if (
            type(wait_seconds) not in {int, float}
            or not isfinite(wait_seconds)
            or wait_seconds < 0
        ):
            raise ManifestError("cleanup_wait_refused")
        functional = False
        try:
            records = self.journal.records()
            latest = {record["step"]: record for record in records}
            functional = all(
                latest.get(step, {}).get("status") == "passed" for step in STAGES[:-1]
            )
            reference = cleanup_reference(self.manifest)

            def observe():
                proofs = sorted(
                    private_directory(self.manifest["cleanup_path"]).iterdir(),
                    key=lambda path: path.lstat().st_mtime_ns,
                )
                if not proofs:
                    return None
                proof = private_read(proofs[-1])
                if not matches_cleanup_reference(proof, reference):
                    raise ManifestError("cleanup_owner_refused")
                flags = proof["cleanup"]
                if type(flags) is not dict:
                    raise ManifestError("cleanup_flags_refused")
                closed = all(
                    flags.get(name + "_closed") is True
                    for name in (
                        "runtime",
                        "native",
                        "backend",
                        "listener",
                        "vite",
                        "bootstrap",
                        "knowledge",
                        "controller",
                    )
                )
                guards = (
                    flags.get("guards_inspected") is True
                    and flags.get("guards_released") is True
                    if self.manifest["guard_custody"] == "lab"
                    else flags.get("guards_inspected") is None
                    and flags.get("guards_released") is None
                )
                complete = proof["complete"] is True and closed and guards
                return self._report(
                    "owned_cleanup_confirmed"
                    if complete
                    else "owner_cleanup_incomplete",
                    functional=functional,
                    cleanup=complete,
                )

            proof_report = observe()
            if proof_report is not None:
                return proof_report
            parent = Path(self.manifest["owner_receipt_path"]).parent
            if parent != Path(self.manifest["transport_path"]).parent:
                raise ManifestError("cleanup_owner_refused")
            private_directory(parent)
            owner = private_read(self.manifest["owner_receipt_path"])
            if not matches_cleanup_reference(owner, reference):
                raise ManifestError("cleanup_owner_refused")
            request_path = parent / "owner-cleanup-request.json"
            try:
                request = private_read(request_path)
            except ManifestError:
                if request_path.exists() or request_path.is_symlink():
                    raise
                try:
                    private_write(request_path, reference)
                except FileExistsError:
                    pass
                request = private_read(request_path)
            if not matches_cleanup_reference(request, reference, exact=True):
                raise ManifestError("cleanup_owner_refused")
            deadline = monotonic() + wait_seconds
            while True:
                proof_report = observe()
                if proof_report is not None:
                    return proof_report
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return self._report("owner_cleanup_pending", functional=functional)
                sleep(min(0.1, remaining))
        except (OSError, ValueError, KeyError, TypeError):
            return self._report("cleanup_read_unavailable", functional=functional)

    def _desktop_step(self, step, record):
        if step == "owned_cleanup":
            return {"canonical": self.cleanup()["cleanup"]}
        root = Path(self.manifest["source_root"])
        node = shutil.which("node")
        if node is None:
            raise ManifestError("node24_required")
        home = Path(self.manifest["transport_path"]).parent / "cli-home"
        home.mkdir(mode=0o700, exist_ok=True)
        private_directory(home)
        environment = {
            "HOME": str(home),
            "PATH": str(Path(node).parent) + ":/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "SIDERA_NATIVE_MANIFEST": str(
                Path(self.manifest["transport_path"]).parent / "manifest.json"
            ),
            "SIDERA_NATIVE_STAGE": step,
            "SIDERA_NATIVE_STEP_ID": record["step_id"],
            "SIDERA_NATIVE_RESUME": "1" if record["observed"]["resume"] else "0",
        }
        argv = [
            node,
            str(root / "apps/web/node_modules/@playwright/test/cli.js"),
            "test",
            "--config",
            "playwright.native.config.ts",
            "native-harness.spec.ts",
            "--project",
            "native-chromium",
        ]
        result = subprocess.run(
            argv,
            cwd=root / "apps/web",
            env=environment,
            capture_output=True,
            timeout=3660,
            check=False,
        )
        if result.returncode != 0:
            raise ManifestError("desktop_stage_failed")
        matching = [
            item
            for item in self.journal.records()
            if item["step"] == step
            and item["step_id"] == record["step_id"]
            and item["status"] == "passed"
        ]
        return (
            {}
            if not matching
            else {
                **matching[-1]["observed"],
                "original_ids": matching[-1]["original_ids"],
            }
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check one explicitly owned native product allocation"
    )
    parser.add_argument("command", choices=("preflight", "run", "resume", "cleanup"))
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        check = NativeCheck(
            read_manifest(args.manifest, allow_ui_change=args.command == "resume")
        )
        report = (
            check.preflight()
            if args.command == "preflight"
            else check.cleanup()
            if args.command == "cleanup"
            else check.run(resume=args.command == "resume")
        )
    except (OSError, ValueError, KeyError, TypeError):
        report = {
            "status": "INCOMPLETE",
            "reason": "manifest_refused",
            "functional": False,
            "cleanup": False,
        }
    print(json.dumps(report, sort_keys=True))
    return (
        0
        if report["status"] in {"READY", "PASS"}
        or args.command == "cleanup"
        and report["cleanup"]
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
