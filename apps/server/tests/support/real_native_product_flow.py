import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from threading import get_ident
from uuid import uuid4, uuid5

from pydantic import SecretStr
from sidera_agents.harnesses.opencode import OpenCodeHarness

from sidera_product.app import create_app
from sidera_product.auth.local_session import write_bootstrap_file
from sidera_product.composition import build_product_runtime
from sidera_product.config import ProductSettings

from .agent_management_desktop import (
    OwnedBootstrapExport,
    _bind_listener,
    _reserve_port,
    _stop_owned_process,
    start_owned_backend,
    start_owned_vite,
)
from .agent_management_inventory import write_evidence, write_private_json
from .controlled_knowledge import OwnedKnowledgeServer
from .native_product_flow import (
    PLANNING_BUDGET,
    SOURCE_PATHS,
    _cleanup_reference,
    _digest,
    _private_directory,
    target_environment,
)
from .native_product_harness import local_git
from .product_targets import ProductTargetError

SOURCE_ROOT = Path(__file__).resolve().parents[4]
REAL_SOURCE_PATHS = SOURCE_PATHS + (
    "apps/server/tests/support/real_native_product_flow.py",
    "apps/server/tests/unit/test_real_native_product_flow.py",
    "apps/web/playwright.native-real.config.ts",
    "apps/web/tests/e2e/native-harness-real.spec.ts",
    "packages/agents/src/sidera_agents/harnesses/opencode.py",
    "packages/agents/src/sidera_agents/harnesses/acp_transport.py",
    "packages/agents/src/sidera_agents/harnesses/opencode_config.py",
    "packages/agents/src/sidera_agents/harnesses/opencode_descriptor.py",
    "apps/server/src/sidera_product/composition.py",
    "apps/server/src/sidera_product/chat/projection.py",
    "apps/server/src/sidera_product/task_tools/tools.py",
    "services/control-plane/src/sidera_control_plane/application/agent_worker_adapter.py",
    "services/control-plane/src/sidera_control_plane/application/agent_planner_adapter.py",
)
REAL_NATIVE_SOURCE_PATHS = REAL_SOURCE_PATHS
_retained_real_labs: list[object] = []

REAL_BUDGET = {
    "total_seconds": 7200,
    "main_turns": 40,
    "planner_runs": 16,
    "worker_sessions": 64,
    "adapter_prompts": 200,
}
INPUT_FIXTURES = {
    "valid_default": {"name": "demo", "port": 8080},
    "valid_false": {"name": "demo", "port": 8080, "enabled": False},
    "port_min": {"name": "demo", "port": 1},
    "port_max": {"name": "demo", "port": 65535},
    "non_object": [],
    "bool_port": {"name": "demo", "port": True},
    "blank_name": {"name": " ", "port": 8080},
    "enabled_nonbool": {"name": "demo", "port": 8080, "enabled": 1},
    "port_zero": {"name": "demo", "port": 0},
    "port_high": {"name": "demo", "port": 65536},
    "port_string": {"name": "demo", "port": "8080"},
    "port_float": {"name": "demo", "port": 8080.0},
    "missing_name": {"port": 8080},
    "missing_port": {"name": "demo"},
    "name_nonstring": {"name": 7, "port": 8080},
}
VERIFY_DEMO = """import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from time import monotonic


def require(condition):
    if not condition:
        raise ValueError("independent_verification_failed")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def main():
    root = Path(__file__).resolve().parent
    deadline = monotonic() + 60
    for name in ("config_contract.json", "config_cli.py", "test_config_cli.py", "integration_report.md"):
        path = root / name
        require(path.is_file() and path.resolve().parent == root and path.stat().st_size <= 262144)
    contract = json.loads((root / "config_contract.json").read_text(), object_pairs_hook=unique_object)
    require(isinstance(contract, dict) and json.dumps(contract.get("exit_codes"), sort_keys=True) == json.dumps({
        "OK": 0, "INVALID_CONFIGURATION": 2, "INVALID_JSON": 3, "INPUT_UNAVAILABLE": 4,
    }, sort_keys=True))
    require(bool((root / "integration_report.md").read_text().strip()))

    def run(arguments):
        remaining = deadline - monotonic()
        require(remaining > 0)
        process = subprocess.Popen(
            [sys.executable, "-B", *arguments], cwd=root,
            env={"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = process.communicate(timeout=remaining)
            return process.returncode, out, err
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    for name in ("valid_default", "valid_false", "port_min", "port_max", "non_object", "bool_port", "blank_name", "enabled_nonbool", "port_zero", "port_high", "port_string", "port_float", "missing_name", "missing_port", "name_nonstring", "malformed", "missing"):
        code, out, err = run(["config_cli.py", "inputs/" + name + ".json"])
        if name in {"valid_default", "valid_false", "port_min", "port_max"}:
            expected = {"ok": True, "config": {"name": "demo", "port": {"port_min": 1, "port_max": 65535}.get(name, 8080), "enabled": name != "valid_false"}}
            expected_code = 0
        else:
            expected_code = 3 if name == "malformed" else 4 if name == "missing" else 2
            expected = {"ok": False, "error": {2: "INVALID_CONFIGURATION", 3: "INVALID_JSON", 4: "INPUT_UNAVAILABLE"}[expected_code]}
        require(code == expected_code and err == b"")
        actual = json.loads(out, object_pairs_hook=unique_object)
        require(json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True))
    for arguments in (["config_cli.py"], ["config_cli.py", "inputs/valid_default.json", "extra"], ["config_cli.py", "inputs"]):
        code, out, err = run(arguments)
        require(code == 4 and err == b"")
        actual = json.loads(out, object_pairs_hook=unique_object)
        require(json.dumps(actual, sort_keys=True) == json.dumps({"ok": False, "error": "INPUT_UNAVAILABLE"}, sort_keys=True))
    code, out, err = run(["-m", "unittest", "discover", "-p", "test_config_cli.py"])
    count = re.search(rb"Ran ([1-9][0-9]*) tests?", err)
    require(code == 0 and count is not None)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("FAIL: independent verification")
        raise SystemExit(1) from None
    print("PASS: independent verification")
"""


@dataclass(frozen=True, slots=True)
class RealNativeEntry:
    executable: Path
    executable_sha256: str
    actual_runtime_version: str
    verification_python: Path
    selected_secret: SecretStr = field(repr=False)
    model_id: str = "opencode-go/deepseek-v4.1-flash"
    credential_source_alias: str = "SIDERA_N23_OPENCODE_API_KEY"


class RealNativeProductFlowLab:
    def __init__(
        self, *, targets, guards, directory, evidence, entry, start_web, owns_guards
    ):
        self.targets, self.guards = targets, guards
        self.directory, self.evidence = directory, evidence
        self.entry = entry
        self.start_web, self.owns_guards = start_web, owns_guards
        self.workspace_id = uuid4()
        self.allocation_id = target_environment(targets)[
            "SIDERA_PRODUCT_TEST_ALLOCATION_ID"
        ]
        self.namespace = uuid5(self.workspace_id, self.allocation_id)
        self.repository = directory / "repository"
        self.workspace_root = directory / "workspaces"
        self.artifact_root = directory / "artifacts"
        self.manifest_path = directory / "manifest.json"
        self.transport_path = directory / "transport.json"
        self.runtime = self.native = self.settings = None
        self.knowledge = None
        self.backend = self.listener = self.vite = self.bootstrap_export = None
        self._owner_thread = get_ident()
        self._started = self._closed = self._release_attempted = False
        self._resource_closed: set[str] = set()
        self._cleanup_request_serviced = False
        self._cleanup_request_result = False
        self.cleanup = {
            "guards_inspected": None,
            "guards_released": None,
            "knowledge_closed": True,
            "controller_closed": True,
        }

    def start(self):
        self._require_owner()
        if self._started or self._closed:
            raise ProductTargetError("real_native_lab_restart_refused")
        _validate_entry(self.entry)
        pins = {}
        try:
            for name in REAL_SOURCE_PATHS:
                source = SOURCE_ROOT / name
                if not source.is_file() or source.resolve() != source:
                    raise ProductTargetError("real_native_source_refused")
                pins[name] = {
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "scope": "ui" if name.startswith("apps/web/") else "producer",
                }
        except OSError:
            raise ProductTargetError("real_native_source_refused") from None
        self._started = True
        try:
            self._start_owned(pins)
        except Exception:  # noqa: BLE001 - retain partial owners without exposing selected secrets
            self._retain()
            raise ProductTargetError("real_native_start_refused") from None

    def _start_owned(self, pins):
        for path in (
            self.repository,
            self.workspace_root,
            self.artifact_root,
            self.directory / "bootstrap",
            self.directory / "checkpoints",
            self.directory / "cleanup",
            self.repository / "inputs",
        ):
            _private_directory(path)
        readme = (
            "# JSON configuration CLI\n\n"
            "Deliver config_contract.json, config_cli.py, test_config_cli.py, and integration_report.md.\n"
            "Use only the Python standard library. CLI: python config_cli.py <input.json>.\n"
            "Require a nonblank string name, a nonboolean integer port in 1..65535, and an optional boolean enabled (default true).\n"
            'Contract JSON exit_codes: {"OK": 0, "INVALID_CONFIGURATION": 2, "INVALID_JSON": 3, "INPUT_UNAVAILABLE": 4}.\n'
            'Success stdout: {"ok": true, "config": {"name": "demo", "port": 8080, "enabled": true}}.\n'
            'Error stdout: {"ok": false, "error": "INVALID_CONFIGURATION"}, INVALID_JSON, or INPUT_UNAVAILABLE.\n'
            "Wrong argument count and unreadable/missing paths use exit 4; malformed JSON uses exit 3; invalid semantics uses exit 2.\n"
            "No extra stdout, logs, environment/secret printing, or tracebacks.\n"
            "Requested worker keys: contract -> parallel cli/tests -> integration.\n"
            "CLI and tests consume the immutable contract result; integration consumes both code results.\n"
            "Integration runs python -B -m unittest discover -p test_config_cli.py and python -B verify_demo.py, then records actual results in its report.\n"
            "Integration writes only integration_report.md; do not create incidental files or bytecode caches.\n"
            "Keep verify_demo.py and inputs byte-identical to this seed.\n"
        )
        for name, body in (("README.md", readme), ("verify_demo.py", VERIFY_DEMO)):
            fd = os.open(
                self.repository / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(fd, "w") as stream:
                stream.write(body)
        for name, value in INPUT_FIXTURES.items():
            write_private_json(self.repository / "inputs" / (name + ".json"), value)
        fd = os.open(
            self.repository / "inputs" / "malformed.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(fd, "w") as stream:
            stream.write("{\n")
        local_git(self.repository, "init", "--initial-branch=main")
        local_git(self.repository, "add", "README.md", "verify_demo.py", "inputs")
        local_git(
            self.repository, "commit", "-m", "Owned JSON CLI verification prerequisite"
        )
        head = local_git(self.repository, "rev-parse", "HEAD")
        self.listener, port = _bind_listener("127.0.0.1", 0)
        web_port = _reserve_port("::1") if self.start_web else port
        self.native = OpenCodeHarness(
            request_timeout_seconds=120,
            prompt_timeout_seconds=900,
            cancel_timeout_seconds=120,
            close_timeout_seconds=120,
            stop_grace_seconds=10,
            parent_environment={},
        )
        by_component = {target.component: target for target in self.targets}
        self.knowledge = OwnedKnowledgeServer(self.workspace_id, include_page_list=True)
        self.cleanup["knowledge_closed"] = False
        self.settings = ProductSettings(
            **{
                name: by_component[component].url.render_as_string(hide_password=False)
                for name, component in (
                    ("product_database_url", "product"),
                    ("agent_database_url", "agents"),
                    ("cp_database_url", "cp"),
                    ("credential_database_url", "credentials"),
                )
            },
            deployment_namespace=self.namespace,
            workspace_id=self.workspace_id,
            local_principal_id="owned-n23-human-" + self.workspace_id.hex,
            workspace_root=self.workspace_root,
            artifact_root=self.artifact_root,
            bind_host="127.0.0.1",
            port=port,
            web_origin=f"http://[::1]:{web_port}"
            if self.start_web
            else f"http://127.0.0.1:{port}",
            agent_poll_interval_seconds=0.25,
            bootstrap_ttl_seconds=3600,
            result_inputs_enabled=True,
            result_integration_enabled=True,
            knowledge_base_url=self.knowledge.url,
            knowledge_token=self.knowledge.token,
            managed_credentials_enabled=False,
            selected_secrets={
                self.entry.credential_source_alias: self.entry.selected_secret
            },
        )
        config = {
            "selectors": target_environment(self.targets),
            "scenario": "main",
            "planning_budget": PLANNING_BUDGET,
            "settings": {
                "workspace_id": str(self.workspace_id),
                "deployment_namespace": str(self.namespace),
                "workspace_root": str(self.workspace_root),
                "artifact_root": str(self.artifact_root),
                "principal_id": self.settings.local_principal_id,
                "harness_id": self.native.harness_id,
                "executable": str(self.entry.executable),
                "max_active": 4,
                "max_sessions": 8,
            },
            "real": {
                "schema_version": 1,
                "explicit_provider_run": True,
                "model_id": self.entry.model_id,
                "provider_env": "OPENCODE_API_KEY",
                "credential_source_alias": self.entry.credential_source_alias,
                "executable_sha256": self.entry.executable_sha256,
                "actual_runtime_version": self.entry.actual_runtime_version,
                "verification_python": str(self.entry.verification_python),
                "budget": REAL_BUDGET,
            },
            "verification": {
                "seed_sha256": hashlib.sha256(
                    (self.repository / "verify_demo.py").read_bytes()
                ).hexdigest(),
                "argv": [str(self.entry.verification_python), "-B", "verify_demo.py"],
                "timeout_seconds": 60,
            },
            "knowledge_scope": "not_configured",
        }
        write_private_json(self.directory / "config.json", config)
        self.manifest = {
            "schema_version": 1,
            "owner_uid": os.getuid(),
            "allocation_id": self.allocation_id,
            "workspace_id": str(self.workspace_id),
            "namespace": str(self.namespace),
            "principal_id": self.settings.local_principal_id,
            "repository_directory": str(self.repository),
            "repository_head": head,
            "source_root": str(SOURCE_ROOT),
            "source_pins": pins,
            "config_path": str(self.directory / "config.json"),
            "config_hash": _digest(config),
            "mode": "real",
            "controlled_bridge": False,
            "controller_url": None,
            "base_url": self.settings.web_origin,
            "backend_url": self.settings.local_url,
            "transport_path": str(self.transport_path),
            "checkpoint_path": str(self.directory / "checkpoints"),
            "cleanup_path": str(self.directory / "cleanup"),
            "owner_receipt_path": str(self.directory / "owner.json"),
            "guard_custody": "lab" if self.owns_guards else "caller",
            "process_identities": {
                "owner_pid": os.getpid(),
                "backend_port": port,
                "web_port": web_port,
            },
            "target_facts": [
                {
                    key: getattr(target, key)
                    for key in (
                        "component",
                        "database",
                        "owner",
                        "marker",
                        "socket",
                        "port",
                    )
                }
                for target in self.targets
            ],
        }
        write_private_json(self.manifest_path, self.manifest)
        write_private_json(
            self.directory / "owner.json", _cleanup_reference(self.manifest)
        )
        self.knowledge.start()
        self.runtime = build_product_runtime(self.settings, harnesses=(self.native,))
        self.app = create_app(runtime=self.runtime)
        try:
            start_owned_backend(
                self.app, self.settings, listener=self.listener, resource_owner=self
            )
        finally:
            if self.backend is not None:
                self.listener = None
        bootstrap_path = self.directory / "bootstrap" / "session.json"
        issue = self.runtime.local_sessions.create_bootstrap()
        write_bootstrap_file(issue, output=bootstrap_path)
        self.bootstrap_export = OwnedBootstrapExport.capture(bootstrap_path)
        write_private_json(
            self.transport_path,
            {
                "schema_version": 1,
                "workspace_id": str(self.workspace_id),
                "bootstrap_file": str(bootstrap_path),
            },
        )
        if self.start_web:
            start_owned_vite(
                tmp_path=self.directory,
                web_root=SOURCE_ROOT / "apps/web",
                backend_url=self.settings.local_url,
                port=web_port,
                resource_owner=self,
            )

    def close(self) -> bool:
        self._require_owner()
        if self._closed:
            return True
        if self._release_attempted:
            return False
        transports = (
            (
                "vite",
                self.vite,
                lambda value: value.poll() is not None or _stop_owned_process(value),
            ),
            ("backend", self.backend, lambda value: value.close()),
            (
                "listener",
                self.listener,
                lambda value: (value.close(), value.fileno() == -1)[1],
            ),
        )
        for name, resource, close in transports:
            if name in self._resource_closed:
                continue
            try:
                complete = resource is None or close(resource) is True
            except Exception:  # noqa: BLE001 - drain ingress before disposing Runtime stores
                complete = False
            self.cleanup[name + "_closed"] = complete
            if not complete:
                self._retain()
                return False
            self._resource_closed.add(name)
        if "runtime" not in self._resource_closed:
            try:
                complete = (
                    self.runtime is None
                    or self.runtime.close(grace_seconds=120) is True
                )
            except Exception:  # noqa: BLE001 - retain a live owner on uncertain normal close
                complete = False
            self.cleanup["runtime_closed"] = complete
            if not complete:
                self._retain()
                return False
            self._resource_closed.add("runtime")
        if self.native is None:
            native_closed = True
        else:
            with self.native._lock:
                native_closed = not self.native._sessions and not self.native._orphaned
        self.cleanup["native_closed"] = native_closed
        if not native_closed:
            self._retain()
            return False
        resources = (
            ("bootstrap", self.bootstrap_export, lambda value: value.close()),
            ("knowledge", self.knowledge, lambda value: value.close()),
        )
        for name, resource, close in resources:
            if name in self._resource_closed:
                continue
            try:
                complete = resource is None or close(resource) is True
            except Exception:  # noqa: BLE001 - uncertain closure retains remaining resources
                complete = False
            self.cleanup[name + "_closed"] = complete
            if not complete:
                self._retain()
                return False
            self._resource_closed.add(name)
        if self.owns_guards:
            try:
                idle = all(
                    all(value == 0 for value in guard.inspect()["idle"].values())
                    for guard in self.guards
                )
            except Exception:  # noqa: BLE001 - no release after an unknown guard read
                idle = False
            self.cleanup["guards_inspected"] = idle
            if not idle:
                self._retain()
                return False
            self._release_attempted = True
            for guard in reversed(self.guards):
                try:
                    released = guard.release() is True
                except Exception:  # noqa: BLE001 - never retry an uncertain guard release
                    released = False
                if not released:
                    self.cleanup["guards_released"] = False
                    self._retain()
                    return False
            self.cleanup["guards_released"] = True
        self._closed = True
        self._write_cleanup()
        return True

    def service_cleanup_request(self) -> bool | None:
        self._require_owner()
        if self._cleanup_request_serviced:
            return self._cleanup_request_result
        if not hasattr(self, "manifest"):
            return None
        path = self.directory / "owner-cleanup-request.json"
        try:
            parent = self.directory.lstat()
            if (
                self.directory.resolve() != self.directory
                or not stat.S_ISDIR(parent.st_mode)
                or stat.S_IMODE(parent.st_mode) != 0o700
                or parent.st_uid != os.getuid()
            ):
                raise ValueError("private_owner_refused")
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid()
                    or info.st_size > 2097152
                ):
                    raise ValueError("private_owner_refused")
                request = json.load(stream)
            reference = _cleanup_reference(self.manifest)
            if (
                type(request) is not dict
                or set(request) != set(reference)
                or any(
                    type(request[key]) is not type(value) or request[key] != value
                    for key, value in reference.items()
                )
            ):
                raise ValueError("private_owner_refused")
        except (OSError, ValueError, KeyError, TypeError):
            raise ProductTargetError("real_native_cleanup_request_refused") from None
        self._cleanup_request_serviced = True
        self._cleanup_request_result = self.close() is True
        return self._cleanup_request_result

    def _require_owner(self):
        if get_ident() != self._owner_thread:
            raise ProductTargetError("real_native_cleanup_owner_thread_refused")

    def _retain(self):
        if not any(owner is self for owner in _retained_real_labs):
            _retained_real_labs.append(self)
        self._write_cleanup()

    def _write_cleanup(self):
        if hasattr(self, "manifest") and (self.directory / "cleanup").is_dir():
            write_evidence(
                self.directory / "cleanup",
                "owned-real",
                {
                    **_cleanup_reference(self.manifest),
                    "complete": self._closed,
                    "cleanup": dict(self.cleanup),
                    "functional_scope": "mainline",
                    "whole_n23_accepted": False,
                },
            )


def _validate_entry(entry):
    try:
        if (
            not isinstance(entry, RealNativeEntry)
            or not isinstance(entry.executable, Path)
            or not entry.executable.is_absolute()
            or ".." in entry.executable.parts
            or not entry.executable.is_file()
            or not os.access(entry.executable, os.X_OK)
            or not isinstance(entry.executable_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", entry.executable_sha256) is None
            or hashlib.sha256(entry.executable.read_bytes()).hexdigest()
            != entry.executable_sha256
            or entry.actual_runtime_version != "1.18.32"
            or entry.model_id != "opencode-go/deepseek-v4.1-flash"
            or entry.credential_source_alias != "SIDERA_N23_OPENCODE_API_KEY"
            or not isinstance(entry.selected_secret, SecretStr)
            or not entry.selected_secret.get_secret_value().strip()
            or not isinstance(entry.verification_python, Path)
            or not entry.verification_python.is_absolute()
            or ".." in entry.verification_python.parts
            or not entry.verification_python.is_file()
            or not os.access(entry.verification_python, os.X_OK)
        ):
            raise ProductTargetError("real_native_entry_refused")
    except (OSError, ValueError, TypeError):
        raise ProductTargetError("real_native_entry_refused") from None


def build_real_native_product_flow_lab(
    targets,
    guards,
    directory,
    evidence,
    *,
    entry: RealNativeEntry,
    start_web: bool,
    owns_guards: bool = False,
) -> RealNativeProductFlowLab:
    _validate_entry(entry)
    target_environment(targets)
    if len(guards) != 4 or any(
        guard.target != target or guard.active is not True
        for guard, target in zip(guards, targets, strict=True)
    ):
        raise ProductTargetError("real_native_guard_custody_refused")
    for guard in guards:
        if any(value != 0 for value in guard.inspect()["idle"].values()):
            raise ProductTargetError("real_native_target_not_idle")
    directory, evidence = _private_directory(directory), _private_directory(evidence)
    if any(directory.iterdir()):
        raise ProductTargetError("real_native_directory_not_empty")
    return RealNativeProductFlowLab(
        targets=targets,
        guards=guards,
        directory=directory,
        evidence=evidence,
        entry=entry,
        start_web=start_web,
        owns_guards=owns_guards,
    )
