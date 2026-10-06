import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid5

import pytest
from fastapi.testclient import TestClient
from sidera_control_plane.application.assignments import AssignmentTarget
from sidera_control_plane.domain.assignment_views import (
    ProfileWorkRunTarget,
    ProfileWorkTarget,
)

from sidera_product.app import create_app
from sidera_product.auth.local_session import CSRF_COOKIE, CSRF_HEADER
from sidera_product.cli import migrate_runtime
from sidera_product.composition import ProductRuntime, build_product_runtime
from sidera_product.config import ProductSettings

from .agent_management_inventory import (
    RowInventory,
    capture_rows,
    compare_rows,
    load_or_capture_original_baseline,
    read_private_json,
    write_evidence,
    write_private_json,
)
from .controlled_native import ControlledHarness, ControlledSecretStore
from .product_lab import ControlledPrincipal
from .product_targets import (
    COMPONENTS,
    ProductTargetError,
    TargetGuard,
    TargetSelection,
    acquire_guards,
    select_targets,
)

ALLOCATION_NAMESPACE = UUID("898937f8-0a89-51ea-bfee-563b8524f4ad")
LOCAL_PRINCIPAL_ID = "n20-agent-management"
_retained_cold_resources: list[object] = []
COLD_PROFILE_WORK_STAGES = frozenset(
    {
        "profile_create",
        "work_seed",
        "profile_revise",
        "status_cas",
        "defaults_cas",
        "final_reads",
    }
)


class ManagementHarness(ControlledHarness):
    harness_id = "controlled-n20-agent-management"


def _profile_stage_paths(
    evidence_directory: Path, workspace_id: UUID, stage: str
) -> tuple[Path, Path]:
    if type(workspace_id) is not UUID or stage not in COLD_PROFILE_WORK_STAGES:
        raise ProductTargetError("cold_profile_stage_refused")
    directory = evidence_directory / "cold-profile-work" / str(workspace_id)
    return (
        directory / f"{stage}.pending.json",
        directory / f"{stage}.complete.json",
    )


def _stage_path_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise ProductTargetError("cold_profile_stage_refused") from None


def _read_profile_stage(
    path: Path,
    *,
    workspace_id: UUID,
    stage: str,
    phase: str,
    intent: dict[str, object],
) -> dict[str, object]:
    try:
        value = read_private_json(path)
    except (OSError, ValueError, ProductTargetError):
        raise ProductTargetError("cold_profile_stage_refused") from None
    fields = {"schema_version", "case", "workspace_id", "stage", "phase", "intent"}
    if phase == "complete":
        fields.add("result")
    if (
        type(value) is not dict
        or set(value) != fields
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("case") != "cold-profile-work"
        or value.get("workspace_id") != str(workspace_id)
        or value.get("stage") != stage
        or value.get("phase") != phase
        or value.get("intent") != intent
        or (phase == "complete" and type(value.get("result")) is not dict)
    ):
        raise ProductTargetError("cold_profile_stage_refused")
    return value


def begin_cold_profile_stage(
    evidence_directory: Path,
    workspace_id: UUID,
    stage: str,
    intent: dict[str, object],
) -> tuple[bool, dict[str, object] | None]:
    pending_path, complete_path = _profile_stage_paths(
        evidence_directory, workspace_id, stage
    )
    pending_exists = _stage_path_exists(pending_path)
    complete_exists = _stage_path_exists(complete_path)
    if complete_exists:
        if not pending_exists:
            raise ProductTargetError("cold_profile_stage_refused")
        _read_profile_stage(
            pending_path,
            workspace_id=workspace_id,
            stage=stage,
            phase="pending",
            intent=intent,
        )
        record = _read_profile_stage(
            complete_path,
            workspace_id=workspace_id,
            stage=stage,
            phase="complete",
            intent=intent,
        )
        return False, record["result"]
    if pending_exists:
        _read_profile_stage(
            pending_path,
            workspace_id=workspace_id,
            stage=stage,
            phase="pending",
            intent=intent,
        )
        return False, None
    record = {
        "schema_version": 1,
        "case": "cold-profile-work",
        "workspace_id": str(workspace_id),
        "stage": stage,
        "phase": "pending",
        "intent": intent,
    }
    try:
        write_private_json(pending_path, record)
    except (OSError, ProductTargetError):
        raise ProductTargetError("cold_profile_stage_refused") from None
    return True, None


def load_cold_profile_stage(
    evidence_directory: Path, workspace_id: UUID, stage: str
) -> tuple[dict[str, object], dict[str, object] | None] | None:
    pending_path, complete_path = _profile_stage_paths(
        evidence_directory, workspace_id, stage
    )
    pending_exists = _stage_path_exists(pending_path)
    complete_exists = _stage_path_exists(complete_path)
    if not pending_exists and not complete_exists:
        return None
    if not pending_exists:
        raise ProductTargetError("cold_profile_stage_refused")
    try:
        pending = read_private_json(pending_path)
    except (OSError, ValueError, ProductTargetError):
        raise ProductTargetError("cold_profile_stage_refused") from None
    if (
        type(pending) is not dict
        or set(pending)
        != {"schema_version", "case", "workspace_id", "stage", "phase", "intent"}
        or type(pending.get("intent")) is not dict
    ):
        raise ProductTargetError("cold_profile_stage_refused")
    intent = pending["intent"]
    _read_profile_stage(
        pending_path,
        workspace_id=workspace_id,
        stage=stage,
        phase="pending",
        intent=intent,
    )
    if not complete_exists:
        return intent, None
    record = _read_profile_stage(
        complete_path,
        workspace_id=workspace_id,
        stage=stage,
        phase="complete",
        intent=intent,
    )
    return intent, record["result"]


def complete_cold_profile_stage(
    evidence_directory: Path,
    workspace_id: UUID,
    stage: str,
    intent: dict[str, object],
    result: dict[str, object],
) -> None:
    pending_path, complete_path = _profile_stage_paths(
        evidence_directory, workspace_id, stage
    )
    if not _stage_path_exists(pending_path):
        raise ProductTargetError("cold_profile_stage_refused")
    _read_profile_stage(
        pending_path,
        workspace_id=workspace_id,
        stage=stage,
        phase="pending",
        intent=intent,
    )
    if _stage_path_exists(complete_path):
        saved = _read_profile_stage(
            complete_path,
            workspace_id=workspace_id,
            stage=stage,
            phase="complete",
            intent=intent,
        )
        if saved["result"] != result:
            raise ProductTargetError("cold_profile_stage_refused")
        return
    record = {
        "schema_version": 1,
        "case": "cold-profile-work",
        "workspace_id": str(workspace_id),
        "stage": stage,
        "phase": "complete",
        "intent": intent,
        "result": result,
    }
    try:
        write_private_json(complete_path, record)
    except (OSError, ProductTargetError):
        raise ProductTargetError("cold_profile_stage_refused") from None


class ManagementPrincipal(ControlledPrincipal):
    def require(self, grant, action, target):
        if action == "assignment.read" and isinstance(
            target, (AssignmentTarget, ProfileWorkTarget, ProfileWorkRunTarget)
        ):
            self.calls.append((grant, action, target))
            if (
                grant == self.identity.grant_id
                and target.workspace_id == self.workspace
                and target.principal_id == self.identity.principal_id
            ):
                return
        super().require(grant, action, target)


@dataclass
class ManagementColdLab:
    targets: tuple[TargetSelection, ...]
    guards: list[TargetGuard]
    runtime: ProductRuntime
    native: ManagementHarness
    principal: ControlledPrincipal
    secrets: ControlledSecretStore
    client: TestClient
    evidence_directory: Path
    owned: dict[str, object] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    _closed: bool = False

    @property
    def workspace_id(self) -> UUID:
        return self.runtime.settings.workspace_id

    @property
    def prefix(self) -> str:
        return f"/agents/v1/workspaces/{self.workspace_id}"

    def exchange_session(self) -> None:
        origin = self.runtime.settings.web_origin
        bootstrap = self.runtime.local_sessions.create_bootstrap()
        self.owned["bootstrap_id"] = str(bootstrap.bootstrap_id)
        response = self.client.post(
            "/product/v1/session/bootstrap",
            json={"token": bootstrap.token.get_secret_value()},
            headers={"Origin": origin},
        )
        if response.status_code != 200:
            raise AssertionError("Controlled local-session exchange failed")
        session_id = response.json().get("session_id")
        csrf = self.client.cookies.get(CSRF_COOKIE)
        if not isinstance(session_id, str) or not isinstance(csrf, str):
            raise AssertionError("Controlled local-session cookies were not issued")
        self.owned["session_id"] = session_id
        self.headers = {"Origin": origin, CSRF_HEADER: csrf}

    def record_profile(self, operation_id: str, result: dict[str, object]) -> None:
        self.owned.setdefault("configuration_operations", {})[operation_id] = {
            key: result[key]
            for key in ("operationId", "state", "target_ref", "result_ref")
            if key in result
        }

    def close(self) -> bool:
        if self._closed:
            return True
        self.client.close()
        complete = self.runtime.close(grace_seconds=10)
        self._closed = complete is True
        return self._closed


def select_management_cold_targets(environ: Mapping[str, str]):
    targets = select_targets(environ)
    if targets is None:
        return None, None
    return targets, management_evidence_directory(environ)


def management_evidence_directory(environ: Mapping[str, str]) -> Path:
    selected = environ.get("SIDERA_PRODUCT_TEST_EVIDENCE_DIRECTORY")
    if selected is None:
        raise ProductTargetError("product_evidence_directory_required")
    directory = Path(selected)
    if not directory.is_absolute() or ".." in directory.parts:
        raise ProductTargetError("product_evidence_directory_refused")
    try:
        metadata = directory.lstat()
    except OSError:
        raise ProductTargetError("product_evidence_directory_refused") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
        or directory.resolve() != directory
    ):
        raise ProductTargetError("product_evidence_directory_refused")
    return directory


def management_workspace_id(allocation_id: str) -> UUID:
    return uuid5(ALLOCATION_NAMESPACE, "n20-agent-management:" + allocation_id)


def validate_cold_heads(guards: list[TargetGuard]) -> str:
    if tuple(guard.target.component for guard in guards) != COMPONENTS:
        raise ProductTargetError("product_guard_order_refused")
    accepted = {
        "product": {"0003_conversations", "0004_configuration_requests"},
        "agents": {"0004_inputs_events"},
        "cp": {"0039_hard_only_input_selections"},
        "credentials": {"0001_connections_credentials"},
    }
    heads: dict[str, str] = {}
    for guard in guards:
        current = guard.initial["inventory"]["heads"]
        if len(current) != 1 or current[0] not in accepted[guard.target.component]:
            raise ProductTargetError("product_migration_head_refused")
        heads[guard.target.component] = current[0]
    return heads["product"]


def build_management_cold_lab(
    targets: tuple[TargetSelection, ...],
    guards: list[TargetGuard],
    tmp_path: Path,
    evidence_directory: Path,
    *,
    workspace_id: UUID,
    principal_id: str = LOCAL_PRINCIPAL_ID,
) -> ManagementColdLab:
    by_component = {target.component: target for target in targets}
    repository = tmp_path / "repository"
    principal = ManagementPrincipal(workspace_id, principal_id, repository)
    native = ManagementHarness()
    secrets = ControlledSecretStore()
    workspace_root = tmp_path / "workspaces"
    artifact_root = tmp_path / "artifacts"
    workspace_root.mkdir(mode=0o700)
    artifact_root.mkdir(mode=0o700)
    settings = ProductSettings(
        product_database_url=by_component["product"].url.render_as_string(
            hide_password=False
        ),
        agent_database_url=by_component["agents"].url.render_as_string(
            hide_password=False
        ),
        cp_database_url=by_component["cp"].url.render_as_string(hide_password=False),
        credential_database_url=by_component["credentials"].url.render_as_string(
            hide_password=False
        ),
        deployment_namespace=ALLOCATION_NAMESPACE,
        workspace_id=workspace_id,
        local_principal_id=principal_id,
        workspace_root=workspace_root,
        artifact_root=artifact_root,
        agent_poll_interval_seconds=0.01,
    )
    runtime = build_product_runtime(
        settings,
        harnesses=(native,),
        request_authorizer=principal,
        product_authorizer=principal,
        secret_store=secrets,
    )
    try:
        client = TestClient(create_app(runtime=runtime), base_url=settings.local_url)
    except Exception:
        runtime.close(grace_seconds=0)
        raise
    return ManagementColdLab(
        targets,
        guards,
        runtime,
        native,
        principal,
        secrets,
        client,
        evidence_directory,
    )


@pytest.fixture
def management_cold_lab(tmp_path):
    targets, evidence_directory = select_management_cold_targets(os.environ)
    if targets is None:
        pytest.skip("NOT_RUN: four explicit product target selections are absent")
    if evidence_directory is None:
        raise ProductTargetError("product_evidence_directory_required")
    allocation_id = os.environ["SIDERA_PRODUCT_TEST_ALLOCATION_ID"]
    guards = acquire_guards(targets)
    lab = None
    released = False
    before: dict[str, RowInventory] | None = None
    try:
        product_head = validate_cold_heads(guards)
        baseline = load_or_capture_original_baseline(
            evidence_directory, allocation_id, guards
        )
        before = baseline.rows
        lab = build_management_cold_lab(
            targets,
            guards,
            tmp_path,
            evidence_directory,
            workspace_id=management_workspace_id(allocation_id),
        )
        if product_head == "0003_conversations":
            migrate_runtime(lab.runtime)
        if not all(lab.runtime.inspect_readiness().values()):
            raise AssertionError("Cold product schemas are not ready")
        status = lab.client.get("/product/v1/status")
        if status.status_code != 200 or status.json().get("lifecycle") != "constructed":
            raise AssertionError("Cold product owner was started unexpectedly")
        if status.json().get("execution", {}).get("ready") is not False:
            raise AssertionError("Cold product owner reported execution ready")
        lab.owned["cold_status"] = status.json()
        lab.owned["row_inventory_before"] = {
            component: inventory.facts() for component, inventory in before.items()
        }
        yield lab
    finally:
        clean = lab is None or lab.close()
        if clean:
            try:
                idle = [guard.inspect()["idle"] for guard in guards]
                comparisons = {}
                if before is not None:
                    after = {
                        guard.target.component: capture_rows(guard) for guard in guards
                    }
                    comparisons = {
                        component: compare_rows(before_inventory, after[component])
                        for component, before_inventory in before.items()
                    }
                harness_calls = (
                    [] if lab is None else [call[0] for call in lab.native.calls]
                )
                clean = all(
                    result["preserved"] for result in comparisons.values()
                ) and all(all(value == 0 for value in item.values()) for item in idle)
                write_evidence(
                    evidence_directory,
                    "cold-row-preservation-after",
                    {
                        "preserved": clean,
                        "comparisons": comparisons,
                        "owned": {} if lab is None else lab.owned,
                        "harness_id": None if lab is None else lab.native.harness_id,
                        "harness_calls": harness_calls,
                        "idle": idle,
                    },
                )
            except Exception:
                clean = False
                _retained_cold_resources.extend((lab, guards))
                raise
        if clean:
            for guard in reversed(guards):
                if not guard.release():
                    clean = False
                    break
            released = clean
        if not released:
            _retained_cold_resources.extend((lab, guards))
            if lab is not None and sys.exc_info()[0] is None:
                pytest.fail("Cold product cleanup or target handoff is incomplete")
