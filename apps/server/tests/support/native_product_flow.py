import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from threading import Event, Thread, get_ident
from time import monotonic
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4, uuid5

import httpx
from pydantic import SecretStr
from sidera_agents.contracts import NativeProcessState

from sidera_product.app import create_app
from sidera_product.auth.local_session import (
    CSRF_COOKIE,
    CSRF_HEADER,
    write_bootstrap_file,
)
from sidera_product.composition import build_product_runtime
from sidera_product.config import ProductSettings

from .agent_management_desktop import (
    OwnedBootstrapExport,
    _bind_listener,
    _ensure_private_context_directory,
    _reserve_port,
    _stop_owned_process,
    start_owned_backend,
    start_owned_vite,
)
from .agent_management_inventory import write_evidence, write_private_json
from .controlled_knowledge import BoundedHTTPServer, OwnedKnowledgeServer
from .controlled_native import ControlledSecretStore
from .native_product_harness import NativeProductJourneyHarness, local_git
from .product_targets import COMPONENTS, ProductTargetError, select_targets

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
PLANNING_BUDGET = {
    "max_context_bytes": 1048576,
    "max_tokens": 200000,
    "max_cost_microunits": 1000000,
    "max_runtime_seconds": 3600,
    "max_revisions": 16,
    "max_nodes": 64,
    "max_concurrency": 4,
}
_retained_native_labs: list[object] = []


def _cleanup_reference(manifest):
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


def target_environment(targets):
    if len(targets) != 4 or tuple(item.component for item in targets) != COMPONENTS:
        raise ProductTargetError("native_targets_refused")
    first = targets[0]
    allocation = first.marker.removesuffix(":" + first.component)
    environ = {
        "SIDERA_PRODUCT_TEST_ALLOCATION_ID": allocation,
        "SIDERA_PRODUCT_TEST_SOCKET_DIRECTORY": first.socket,
        "SIDERA_PRODUCT_TEST_PORT": str(first.port),
    }
    for target in targets:
        prefix = "SIDERA_PRODUCT_TEST_" + target.component.upper() + "_"
        environ.update(
            {
                prefix + "DATABASE_URL": target.url.render_as_string(
                    hide_password=False
                ),
                prefix + "EXPECTED_DATABASE": target.database,
                prefix + "EXPECTED_OWNER": target.owner,
                prefix + "EXPECTED_MARKER": target.marker,
            }
        )
    if select_targets(environ) != targets:
        raise ProductTargetError("native_targets_refused")
    return environ


def _private_directory(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.resolve() != path:
        raise ProductTargetError("native_directory_refused")
    _ensure_private_context_directory(path)
    return path


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def verify_result_repository(lab, candidate: dict, checks: list[dict]) -> dict:
    """Correlate immutable capture with verification, without applying its checkout."""
    expected = {
        "native_value.py": b"VALUE = 42\n",
        "hard_value.py": b"from native_value import VALUE\nANSWER = VALUE\n",
        "integration.txt": b"Integrated 42\n",
    }

    def refuse():
        raise ValueError("native_result_repository_refused")

    def oid(value):
        if (
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None
        ):
            refuse()
        return value

    def git(*arguments):
        try:
            return subprocess.run(
                ["/usr/bin/git", *arguments],
                cwd=lab.repository,
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(lab.repository),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_OPTIONAL_LOCKS": "0",
                },
                check=True,
                capture_output=True,
                timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            refuse()

    commit, tree = (
        oid(candidate.get("result_commit")),
        oid(candidate.get("result_tree")),
    )
    original = oid(lab.manifest.get("repository_head"))
    try:
        candidate_id = str(UUID(candidate["candidate_id"]))
        code_result_id = str(UUID(candidate["code_result_id"]))
        execution_id = str(UUID(candidate["integration_execution_id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        refuse()
    if (
        git("cat-file", "-t", commit) != b"commit\n"
        or git("rev-parse", "--verify", commit + "^{commit}").strip().decode() != commit
        or git("rev-parse", "--verify", commit + "^{tree}").strip().decode() != tree
    ):
        refuse()
    matched = [
        check
        for check in checks
        if check.get("candidate_id") == candidate_id
        and check.get("check_key") == "owned-check"
        and check.get("status") == "passed"
        and type(check.get("exit_code")) is int
        and check["exit_code"] == 0
        and check.get("actual_commit") == commit
        and check.get("actual_tree") == tree
    ]
    if not matched:
        refuse()
    try:
        verification_id = str(UUID(matched[0]["verification_id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        refuse()
    if (
        git("rev-parse", "HEAD").strip().decode() != original
        or git("status", "--porcelain=v1", "--untracked-files=all")
        or any(
            (lab.repository / name).exists() or (lab.repository / name).is_symlink()
            for name in expected
        )
    ):
        refuse()
    hashes = {}
    for name, body in expected.items():
        revision = commit + ":" + name
        if git("cat-file", "-s", revision) != str(len(body)).encode() + b"\n":
            refuse()
        raw = git("cat-file", "blob", revision)
        if raw != body:
            refuse()
        hashes[name] = hashlib.sha256(raw).hexdigest()
    return {
        "candidate_id": candidate_id,
        "code_result_id": code_result_id,
        "integration_execution_id": execution_id,
        "verification_id": verification_id,
        "original_head": original,
        "result_commit": commit,
        "result_tree": tree,
        "original_checkout_clean": True,
        "file_sha256": hashes,
    }


class NativeControlBridge:
    """Only controlled native observation/gates, never business operations."""

    def __init__(self, lab):
        self.lab = lab
        self.token = SecretStr(uuid4().hex)
        self._stop, self._thread, self._closed = Event(), None, False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                path = urlsplit(self.path).path
                status, body = 404, {"reason": "native_reference_refused"}
                if not hmac.compare_digest(
                    self.headers.get("Authorization", ""),
                    "Bearer " + owner.token.get_secret_value(),
                ):
                    status = 401
                elif self.headers.get("X-Sidera-Native-Workspace") != str(
                    lab.workspace_id
                ):
                    status = 403
                elif self.command == "GET" and self.path == "/facts":
                    status, body = 200, lab.stage_facts()
                elif self.command == "POST" and self.path in {
                    "/release-prompt",
                    "/confirm-exit",
                    "/mark-exit-unknown",
                }:
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= 1024:
                            raise ValueError
                        value = json.loads(self.rfile.read(length))
                        fields = (
                            {"run_id", "input_operation_id"}
                            if path == "/release-prompt"
                            else {"run_id"}
                        )
                        if type(value) is not dict or set(value) != fields:
                            raise ValueError
                        if type(value["run_id"]) is not str:
                            raise ValueError
                        run = UUID(value["run_id"])
                        operation = (
                            UUID(value["input_operation_id"])
                            if path == "/release-prompt"
                            else None
                        )
                        if run != lab.native.original_run_id or (
                            path == "/mark-exit-unknown"
                            and getattr(lab, "scenario", None) != "transition"
                        ):
                            status = 404
                        else:
                            if path == "/mark-exit-unknown":
                                with lab.native.lock:
                                    inspection = lab.native.inspections.get(run)
                                    changed = (
                                        run in lab.native.handles
                                        and lab.native.roles.get(run) == "worker"
                                        and inspection is not None
                                        and inspection.process_state
                                        == NativeProcessState.ALIVE
                                        and run not in lab.native.close_attempts
                                    )
                                    if changed and run not in lab.native.unknown_exits:
                                        lab.native.unknown_exits.add(run)
                                        lab.record(
                                            "transition_source_unknown_ready",
                                            {"run_id": str(run)},
                                        )
                            else:
                                changed = (
                                    lab.native.release_accepted_prompt(run, operation)
                                    if operation is not None
                                    else lab.native.confirm_original_exit(run)
                                )
                            status, body = (
                                (200 if changed else 409),
                                {"confirmed": changed},
                            )
                    except (ValueError, TypeError, KeyError, OSError):
                        status = 400
                encoded = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._server = BoundedHTTPServer(("127.0.0.1", 0), Handler)
        self._server.timeout = 0.05
        self.url = f"http://127.0.0.1:{self._server.server_port}"

    def start(self):
        if self._thread is not None or self._closed:
            raise ProductTargetError("native_bridge_restart_refused")

        def serve():
            while not self._stop.is_set():
                self._server.handle_request()

        self._thread = Thread(target=serve, name="n22-owned-native-controller")
        self._thread.start()

    def close(self):
        if self._closed:
            return True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(3)
            if self._thread.is_alive():
                return False
        self._server.server_close()
        self._closed = True
        return True


class NativeProductFlowLab:
    def __init__(
        self,
        *,
        targets,
        guards,
        directory,
        evidence,
        start_web,
        owns_guards=False,
        scenario="main",
    ):
        if type(scenario) is not str or scenario not in {"main", "transition"}:
            raise ProductTargetError("native_scenario_refused")
        self.scenario = scenario
        self.targets, self.guards = targets, guards
        self.directory, self.evidence = Path(directory), Path(evidence)
        self.start_web, self.owns_guards = start_web, owns_guards
        self.workspace_id, self.namespace = uuid4(), uuid4()
        self.repository = self.directory / "repository"
        self.workspace_root, self.artifact_root = (
            self.directory / "workspaces",
            self.directory / "artifacts",
        )
        self.manifest_path, self.transport_path = (
            self.directory / "manifest.json",
            self.directory / "transport.json",
        )
        self.runtime = self.native = self.settings = self.knowledge = (
            self.controller
        ) = None
        self.backend = self.vite = self.bootstrap_export = self.listener = None
        self.owned, self.facts, self.cleanup = {}, {}, {}
        self._closed = self._release_attempted = False
        self._resource_closed: set[str] = set()
        self._owner_thread = get_ident()
        self._cleanup_request_serviced = False
        self._cleanup_request_result = False
        self.cleanup.update(guards_inspected=None, guards_released=None)

    def service_cleanup_request(self) -> bool | None:
        if get_ident() != self._owner_thread:
            raise ProductTargetError("native_cleanup_owner_thread_refused")
        if self._cleanup_request_serviced:
            return self._cleanup_request_result
        if not hasattr(self, "manifest"):
            return None
        try:
            parent = Path(self.manifest["owner_receipt_path"]).parent
            info = parent.lstat()
            if (
                parent != self.directory
                or not parent.is_absolute()
                or parent.resolve(strict=True) != parent
                or not stat.S_ISDIR(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.getuid()
            ):
                raise ValueError("private_directory_refused")
            try:
                fd = os.open(
                    parent / "owner-cleanup-request.json", os.O_RDONLY | os.O_NOFOLLOW
                )
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
                    raise ValueError("private_file_refused")
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
                raise ValueError("private_reference_refused")
        except (OSError, ValueError, TypeError, KeyError):
            raise ProductTargetError("native_cleanup_request_refused") from None
        self._cleanup_request_serviced = True
        self._cleanup_request_result = self.close() is True
        return self._cleanup_request_result

    def start(self):
        if self.settings is not None or self._closed:
            raise ProductTargetError("native_lab_restart_refused")
        self._owner_thread = get_ident()
        environ = target_environment(self.targets)
        self.allocation_id = environ["SIDERA_PRODUCT_TEST_ALLOCATION_ID"]
        self.namespace = uuid5(self.workspace_id, self.allocation_id)
        for path in (
            self.repository,
            self.workspace_root,
            self.artifact_root,
            self.directory / "bootstrap",
            self.directory / "checkpoints",
            self.directory / "cleanup",
        ):
            _private_directory(path)
        if any(self.repository.iterdir()):
            raise ProductTargetError("native_repository_not_empty")
        check = (
            "from pathlib import Path\n"
            "from native_value import VALUE\n"
            "from hard_value import ANSWER\n"
            "assert VALUE == ANSWER == 42\n"
            "assert Path('integration.txt').read_text() == 'Integrated 42\\n'\n"
        )
        (self.repository / "check.py").write_text(check)
        os.chmod(self.repository / "check.py", 0o600)
        local_git(self.repository, "init", "--initial-branch=main")
        local_git(self.repository, "add", "check.py")
        local_git(
            self.repository, "commit", "-m", "Owned native verification prerequisite"
        )
        head = local_git(self.repository, "rev-parse", "HEAD")
        self.listener, port = _bind_listener("127.0.0.1", 0)
        web_port = _reserve_port("::1") if self.start_web else port
        self.knowledge = OwnedKnowledgeServer(self.workspace_id, include_page_list=True)
        self.native = NativeProductJourneyHarness()
        by_component = {target.component: target for target in self.targets}
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
            local_principal_id="owned-n22-human-" + self.workspace_id.hex,
            workspace_root=self.workspace_root,
            artifact_root=self.artifact_root,
            bind_host="127.0.0.1",
            port=port,
            web_origin=f"http://[::1]:{web_port}"
            if self.start_web
            else f"http://127.0.0.1:{port}",
            agent_poll_interval_seconds=0.01,
            bootstrap_ttl_seconds=3600,
            result_inputs_enabled=True,
            result_integration_enabled=True,
            knowledge_base_url=self.knowledge.url,
            knowledge_token=self.knowledge.token,
        )
        config = {
            "scenario": self.scenario,
            "selectors": environ,
            "planning_budget": PLANNING_BUDGET,
            "settings": {
                "workspace_id": str(self.workspace_id),
                "deployment_namespace": str(self.namespace),
                "workspace_root": str(self.workspace_root),
                "artifact_root": str(self.artifact_root),
                "principal_id": self.settings.local_principal_id,
                "harness_id": self.native.harness_id,
                "executable": sys.executable,
                "max_active": 4,
                "max_sessions": 4,
            },
        }
        write_private_json(self.directory / "config.json", config)
        self.runtime = build_product_runtime(
            self.settings,
            harnesses=(self.native,),
            secret_store=ControlledSecretStore(),
        )
        self.native.runtime = self.runtime
        self.controller = NativeControlBridge(self)
        self.app = create_app(runtime=self.runtime)
        source_root = Path(__file__).resolve().parents[4]
        pins = {
            path: {
                "sha256": hashlib.sha256((source_root / path).read_bytes()).hexdigest(),
                "scope": "ui" if path.startswith("apps/web/") else "producer",
            }
            for path in SOURCE_PATHS
        }
        self.manifest = {
            "schema_version": 1,
            "owner_uid": os.getuid(),
            "allocation_id": self.allocation_id,
            "workspace_id": str(self.workspace_id),
            "namespace": str(self.namespace),
            "principal_id": self.settings.local_principal_id,
            "repository_directory": str(self.repository),
            "repository_head": head,
            "source_root": str(source_root),
            "source_pins": pins,
            "config_path": str(self.directory / "config.json"),
            "config_hash": _digest(config),
            "mode": "fake",
            "controlled_bridge": True,
            "base_url": self.settings.web_origin,
            "guard_custody": "lab" if self.owns_guards else "caller",
            "backend_url": self.settings.local_url,
            "controller_url": self.controller.url,
            "transport_path": str(self.transport_path),
            "checkpoint_path": str(self.directory / "checkpoints"),
            "cleanup_path": str(self.directory / "cleanup"),
            "owner_receipt_path": str(self.directory / "owner.json"),
            "process_identities": {
                "owner_pid": os.getpid(),
                "backend_port": port,
                "web_port": web_port,
            },
            "target_facts": [
                {
                    "component": target.component,
                    "database": target.database,
                    "owner": target.owner,
                    "marker": target.marker,
                    "socket": target.socket,
                    "port": target.port,
                }
                for target in self.targets
            ],
        }
        write_private_json(self.manifest_path, self.manifest)
        self.knowledge.start()
        self.controller.start()
        try:
            start_owned_backend(
                self.app, self.settings, listener=self.listener, resource_owner=self
            )
        finally:
            if self.backend is not None:
                self.listener = None
        if self.start_web:
            start_owned_vite(
                tmp_path=self.directory,
                web_root=source_root / "apps/web",
                backend_url=self.settings.local_url,
                port=web_port,
                knowledge_port=urlsplit(self.knowledge.url).port,
                knowledge_token=self.knowledge.token,
                resource_owner=self,
            )
        issue = self.runtime.local_sessions.create_bootstrap()
        bootstrap_path = self.directory / "bootstrap" / "session.json"
        write_bootstrap_file(issue, output=bootstrap_path)
        self.bootstrap_export = OwnedBootstrapExport.capture(bootstrap_path)
        write_private_json(
            self.transport_path,
            {
                "schema_version": 1,
                "workspace_id": str(self.workspace_id),
                "bootstrap_file": str(bootstrap_path),
                "controller_token": self.controller.token.get_secret_value(),
            },
        )
        write_private_json(
            self.directory / "owner.json",
            {
                **_cleanup_reference(self.manifest),
                "vite_pid": None if self.vite is None else self.vite.pid,
                "backend_port": port,
                "controller_port": urlsplit(self.controller.url).port,
                "knowledge_port": urlsplit(self.knowledge.url).port,
            },
        )
        self.facts["started"] = True

    def stage_facts(self):
        return {
            "schema_version": 1,
            "workspace_id": str(self.workspace_id),
            "guard_custody": "lab" if self.owns_guards else "caller",
            "cleanup": dict(self.cleanup),
            "native": {} if self.native is None else self.native.public_facts(),
            "knowledge": {} if self.knowledge is None else self.knowledge.facts(),
            "functional": dict(self.facts),
        }

    def record(self, stage, observed):
        self.facts[stage] = observed
        return write_evidence(
            self.evidence,
            "n22-" + stage,
            {"workspace_id": str(self.workspace_id), "observed": observed},
        )

    def close(self):
        if self._closed:
            return True
        if self._release_attempted:
            return False
        resources = (
            ("runtime", self.runtime, lambda value: value.close(grace_seconds=15)),
            ("backend", self.backend, lambda value: value.close()),
            (
                "listener",
                self.listener,
                lambda value: (value.close(), value.fileno() == -1)[1],
            ),
            (
                "vite",
                self.vite,
                lambda value: value.poll() is not None or _stop_owned_process(value),
            ),
            ("bootstrap", self.bootstrap_export, lambda value: value.close()),
            ("knowledge", self.knowledge, lambda value: value.close()),
            ("controller", self.controller, lambda value: value.close()),
        )
        for name, resource, close in resources:
            if name in self._resource_closed:
                continue
            try:
                complete = resource is None or close(resource) is True
            except Exception:  # noqa: BLE001 - unknown closure failure retains its owner
                complete = False
            self.cleanup[name + "_closed"] = complete
            if not complete:
                self._retain()
                return False
            self._resource_closed.add(name)
        self.cleanup["native_closed"] = self.native is None or all(
            item.process_state == "gone" for item in self.native.inspections.values()
        )
        if not self.cleanup["native_closed"]:
            self._retain()
            return False
        if self.owns_guards:
            try:
                inspected = [guard.inspect() for guard in self.guards]
                idle = all(
                    all(value == 0 for value in item["idle"].values())
                    for item in inspected
                )
            except Exception:  # noqa: BLE001 - unavailable guard inspection forbids release
                idle = False
            self.cleanup["guards_inspected"] = idle
            if not idle:
                self._retain()
                return False
            self._release_attempted = True
            for guard in reversed(self.guards):
                try:
                    released = guard.release() is True
                except Exception:  # noqa: BLE001 - uncertain release retains remaining owners
                    released = False
                if not released:
                    self.cleanup["guards_released"] = False
                    self._retain()
                    return False
            self.cleanup["guards_released"] = True
        self._closed = True
        self._write_cleanup()
        return True

    def _retain(self):
        if not any(item is self for item in _retained_native_labs):
            _retained_native_labs.append(self)
        self._write_cleanup()

    def _write_cleanup(self):
        if (self.directory / "cleanup").is_dir() and hasattr(self, "manifest"):
            write_evidence(
                self.directory / "cleanup",
                "owned",
                {
                    **_cleanup_reference(self.manifest),
                    "complete": self._closed,
                    "cleanup": dict(self.cleanup),
                },
            )


def build_native_product_flow_lab(
    targets,
    guards,
    directory,
    evidence,
    *,
    start_web,
    owns_guards=False,
    scenario="main",
):
    if type(scenario) is not str or scenario not in {"main", "transition"}:
        raise ProductTargetError("native_scenario_refused")
    target_environment(targets)
    if len(guards) != 4 or any(
        guard.target != target or not guard.active
        for guard, target in zip(guards, targets, strict=True)
    ):
        raise ProductTargetError("native_guard_custody_refused")
    for guard in guards:
        facts = guard.inspect()
        if any(value != 0 for value in facts["idle"].values()):
            raise ProductTargetError("native_target_not_idle")
    directory, evidence = _private_directory(directory), _private_directory(evidence)
    if any(directory.iterdir()):
        raise ProductTargetError("native_directory_not_empty")
    return NativeProductFlowLab(
        targets=targets,
        guards=guards,
        directory=directory,
        evidence=evidence,
        start_web=start_web,
        owns_guards=owns_guards,
        scenario=scenario,
    )


class NativeProductClient:
    """Current-human public API client for the API proof, not the desktop writers."""

    def __init__(self, lab):
        self.lab = lab
        self.client = httpx.Client(
            base_url=lab.settings.local_url,
            trust_env=False,
            timeout=10,
            headers={"Origin": lab.settings.web_origin},
        )
        from sidera_product.auth.local_session import read_bootstrap_file

        token = read_bootstrap_file(lab.bootstrap_export.location).get_secret_value()
        self.request("POST", "/product/v1/session/bootstrap", {"token": token})
        csrf = self.client.cookies.get(CSRF_COOKIE)
        if not csrf:
            raise AssertionError("Native local session has no CSRF binding")
        self.client.headers[CSRF_HEADER] = csrf
        self.session = self.request("GET", "/product/v1/session")
        assert self.session["workspace_id"] == str(lab.workspace_id)
        assert self.session["principal_id"] == lab.settings.local_principal_id
        self.prefix = f"/api/v1/workspaces/{lab.workspace_id}/"

    def request(self, method, path, body=None):
        response = self.client.request(method, path, json=body)
        assert 200 <= response.status_code < 300, (
            f"Native public request failed: {method} {path} {response.status_code}"
        )
        return response.json()

    def get(self, path):
        return self.request("GET", self.prefix + path)

    def wait(self, read, ready, stage, *, seconds=180):
        deadline, wake = monotonic() + seconds, Event()
        while True:
            value = read()
            if ready(value):
                return value
            if monotonic() >= deadline:
                self.lab.record(
                    "failure",
                    {
                        "stage": stage,
                        "reason": "original_stage_timeout",
                        "time": datetime.now(UTC).isoformat(),
                    },
                )
                raise AssertionError("Original native stage did not finish: " + stage)
            wake.wait(0.05)

    def wait_for_source_context(self, worker_item_id):
        self.wait(
            lambda: self.lab.native.source_entered.is_set(),
            bool,
            "source-prompt-entered",
        )
        workspace, item = str(self.lab.workspace_id), str(worker_item_id)
        original_run = str(self.lab.native.original_run_id)
        path = f"worker-items/{item}/native-context"

        def current(value):
            address, execution, run = (
                value.get("context"),
                value.get("execution"),
                value.get("run"),
            )
            return (
                isinstance(address, dict)
                and isinstance(execution, dict)
                and isinstance(run, dict)
                and value.get("workspace_id") == workspace
                and value.get("worker_item_id") == item
                and value.get("execution_mode") == "native_profile"
                and value.get("input_enabled") is True
                and address.get("worker_item_id") == item
                and address.get("run_id") == original_run
                and execution.get("workspace_id") == workspace
                and execution.get("worker_item_id") == item
                and execution.get("execution_id") == address.get("execution_id")
                and execution.get("run_id") == original_run
                and run.get("profile_ref", {}).get("workspace_id") == workspace
                and execution.get("profile_ref") == run.get("profile_ref")
            )

        def read():
            first = self.get(path)
            if not current(first):
                return None
            address = first["context"]
            trace = self.get(
                f"worker-items/{item}/executions/{address['execution_id']}/native-trace"
                "?after_sequence=0&limit=100"
            )
            page = self.request(
                "GET",
                "/api/v1/gateway/sessions?"
                + urlencode(
                    {
                        "workspace_id": workspace,
                        "session_id": address["session_id"],
                        "limit": 1,
                    }
                ),
            )
            latest = self.get(path)
            if not current(latest) or latest["context"] != address:
                return None
            events, cursor = trace["events"], trace["next_sequence"]
            if (
                trace["workspace_id"] != workspace
                or trace["worker_item_id"] != item
                or trace["execution_id"] != address["execution_id"]
                or trace["run_id"] != original_run
                or trace["profile_ref"] != latest["run"]["profile_ref"]
                or trace["has_more"] is not False
                or type(cursor) is not int
                or cursor <= 0
                or not events
                or events[-1]["sequence"] != cursor
                or any(event["run_id"] != original_run for event in events)
                or not any(event["kind"] == "response.started" for event in events)
                or len(page["items"]) != 1
            ):
                return None
            session = page["items"][0]
            if (
                session["workspace_id"] != workspace
                or session["execution_id"] != address["execution_id"]
                or session["id"] != address["session_id"]
                or session["worker_id"] != address["worker_id"]
                or session["adapter_session_id"] != f"agent-run:{original_run}"
                or session["status"] != "active"
                or session["last_sequence"] < cursor
                or session["version"] != address["session_version"]
            ):
                return None
            return latest

        return self.wait(
            read, lambda value: value is not None, "original-source-input-projection"
        )

    def command(
        self, kind, target_type, target_id, version, payload, *, command_id=None
    ):
        command_id = str(command_id or uuid4())
        body = {
            "command_id": command_id,
            "workspace_id": str(self.lab.workspace_id),
            "command_type": kind,
            "target_type": target_type,
            "target_id": str(target_id),
            "expected_version": version,
            "actor": {"type": "user", "id": self.session["principal_id"]},
            "idempotency_key": command_id,
            "correlation_id": command_id,
            "payload": payload,
        }
        self.lab.record(
            "command-intent",
            {
                "command_id": command_id,
                "kind": kind,
                "target_id": str(target_id),
                "version": version,
                "fingerprint": _digest(body),
            },
        )
        result = self.request("POST", "/api/v1/commands", body)
        assert result["command_id"] == command_id
        saved = self.get("commands/" + command_id)
        assert saved["command_id"] == command_id and saved["status"] == "applied"
        assert saved["result"] == result
        return result

    def configure(self):
        prefix = f"/agents/v1/workspaces/{self.lab.workspace_id}"
        workspace = str(self.lab.workspace_id)
        metadata = self.request(
            "GET", f"/product/v1/workspaces/{workspace}/tool-connections"
        )
        assert isinstance(metadata, list) and len(metadata) == 1
        connection = metadata[0]
        assert (
            connection["workspace_id"] == workspace
            and connection["name"] == "SIDERA Task Tools"
            and connection["availability"] == "configured"
            and str(UUID(connection["connection_id"])) == connection["connection_id"]
        )

        def created_ref(result, operation_id, field):
            ref = result["result_ref"]
            assert (
                result["operationId"] == operation_id
                and result["state"] in {"succeeded", "reconciled"}
                and isinstance(ref, dict)
                and ref == result["target_ref"]
                and set(ref) == {"workspace_id", field, "revision"}
                and ref["workspace_id"] == workspace
                and type(ref["revision"]) is int
                and ref["revision"] == 1
                and str(UUID(ref[field])) == ref[field]
            )
            return ref

        runtime = self.request(
            "POST",
            prefix + "/runtimes",
            {
                "harness_id": self.lab.native.harness_id,
                "executable": sys.executable,
                "max_active": 4,
                "max_sessions": 4,
            },
        )
        assert runtime["ref"]["workspace_id"] == workspace
        resource_operation = str(uuid4())
        self.lab.record(
            "task-tool-resource-intent",
            {
                "operation_id": resource_operation,
                "connection_id": connection["connection_id"],
            },
        )
        resource_result = self.request(
            "POST",
            prefix + "/resources",
            {
                "operationId": resource_operation,
                "draft": {
                    "name": "SIDERA Task Tools",
                    "kind": "mcp",
                    "settings": {"connection_id": connection["connection_id"]},
                    "secret_refs": [],
                },
            },
        )
        resource = created_ref(resource_result, resource_operation, "resource_id")
        profile_operation = str(uuid4())
        self.lab.record(
            "profile-configuration-intent",
            {"operation_id": profile_operation, "resource_ref": resource},
        )
        profile_result = self.request(
            "POST",
            prefix + "/profiles",
            {
                "operationId": profile_operation,
                "draft": {
                    "name": "Native composed journey",
                    "description": "",
                    "runtime_ref": runtime["ref"],
                    "instructions": "Execute the authorized native work",
                    "resource_refs": [resource],
                    "model_id": None,
                    "thinking": None,
                    "max_active": 4,
                    "environment": {},
                    "secret_refs": [],
                    "native_settings": {},
                },
            },
        )
        profile = created_ref(profile_result, profile_operation, "profile_id")
        self.request(
            "PUT",
            f"/product/v1/workspaces/{self.lab.workspace_id}/defaults",
            {
                "expected_version": 0,
                "main_profile_id": profile["profile_id"],
                "planner_profile_id": profile["profile_id"],
                "worker_profile_id": profile["profile_id"],
            },
        )
        setup = self.request(
            "POST",
            f"/product/v1/workspaces/{self.lab.workspace_id}/setup",
            {
                "repository_directory": str(self.lab.repository),
                "expected_version": 0,
                "planning_budget": PLANNING_BUDGET,
                "planner_agent_ref": profile,
                "verification_checks": [
                    {
                        "key": "owned-check",
                        "version": 1,
                        "argv": [sys.executable, "-B", "check.py"],
                        "cwd": ".",
                        "environment_ref": "env://owned-test",
                        "prepare_argvs": [],
                        "timeout_seconds": 10,
                        "success_exit_codes": [0],
                        "report_paths": [],
                        "required": True,
                    }
                ],
            },
        )
        self.lab.record(
            "configuration",
            {
                "runtime_ref": runtime["ref"],
                "profile_ref": profile,
                "setup": setup,
                "profile_create_operation_id": profile_operation,
                "profile_create_ref": profile,
                "configuration_operation_id": profile_operation,
                "task_tool_resource_operation_id": resource_operation,
                "task_tool_resource_ref": resource,
                "task_tool_connection_id": connection["connection_id"],
            },
        )
        return profile, setup

    def propose(self):
        thread = self.request(
            "POST",
            "/main-agent/v1/threads",
            {"title": "Native composed journey", "contextRefs": []},
        )
        turn = self.request(
            "POST",
            f"/main-agent/v1/threads/{thread['id']}/turns",
            {"input": "Build the checked native result", "operationId": str(uuid4())},
        )
        goal = self.wait(
            lambda: self.lab.native.public_facts().get("goal"),
            lambda value: value is not None,
            "original-main-goal",
        )
        planning = self.wait(
            lambda: self.get("planning-requests/" + goal["planning_request_id"]),
            lambda item: item["status"] == "proposal_ready",
            "original-proposal",
        )
        assert not any(
            item["role"] == "worker" for item in self.lab.native.public_facts()["opens"]
        )
        self.lab.record(
            "goal", {"thread_id": thread["id"], "turn_id": turn["id"], **goal}
        )
        return goal, planning

    def confirm(self, goal, planning, profile, setup):
        proposal = self.get(
            f"planning-requests/{goal['planning_request_id']}/proposals/1"
        )
        choices = [
            {
                "target_type": "subtask",
                "target_key": "subtask-one",
                "assignee_kind": "human",
                "assignee_id": self.session["principal_id"],
                "profile_revision": None,
                "expected_binding_version": None,
            }
        ]
        choices += [
            {
                "target_type": "worker_item",
                "target_key": key,
                "assignee_kind": "profile",
                "assignee_id": profile["profile_id"],
                "profile_revision": profile["revision"],
                "expected_binding_version": None,
            }
            for key in ("source", "hard-only", "integration")
        ]
        command = self.command(
            "ConfirmPlanningProposal",
            "planning_request",
            goal["planning_request_id"],
            planning["version"],
            {
                "proposal_revision": 1,
                "proposal_content_hash": proposal["content_hash"],
                "profile_id": setup["planning_policy_ref"]["policy_id"],
                "profile_version": setup["planning_policy_ref"]["version"],
                "assignee_bindings": choices,
            },
        )
        materialization = self.get(
            f"planning-requests/{goal['planning_request_id']}/materialization"
        )
        self.lab.record(
            "confirmation",
            {
                "command_id": command["command_id"],
                "worker_item_mapping": materialization["worker_item_mapping"],
            },
        )
        return materialization["worker_item_mapping"]
