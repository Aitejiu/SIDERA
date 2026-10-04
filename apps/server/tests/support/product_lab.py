import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import SecretStr
from sidera_agents.contracts import (
    ExecutionWorkspace,
    ProfileDraft,
    ProfileRef,
    RunStart,
    SecretBinding,
)
from sidera_agents.errors import AgentError
from sidera_control_plane.application.planning_commands import PlanningCommandTarget
from sidera_control_plane.composition.native_runtime import (
    ControlPlaneIdentity,
    ControlPlaneRequestTarget,
    WorkspaceSetupRequest,
)
from sidera_control_plane.domain.planning_profiles import PlanningBudgetPolicy

from sidera_product.composition import build_product_runtime
from sidera_product.config import ProductSettings

from .controlled_native import ControlledHarness, ControlledSecretStore

CONTROLLED_PLANNING_BUDGET = PlanningBudgetPolicy(
    max_context_bytes=1_048_576,
    max_tokens=65_536,
    max_cost_microunits=10_000_000,
    max_runtime_seconds=3600,
    max_revisions=3,
    max_nodes=64,
    max_concurrency=3,
)


class ControlledPrincipal:
    def __init__(self, workspace, principal, repository):
        self.workspace = workspace
        self.identity = ControlPlaneIdentity(principal, uuid4(), "user")
        self.repository = repository
        self.lookups = []
        self.calls = []
        self.workspaces = set()
        self.secrets = set()
        self.planning_selection = None

    def identity_for_grant(self, grant):
        self.lookups.append(grant)
        return self.identity if grant == self.identity.grant_id else None

    def authenticate_request(self, request):
        if request.headers.get("authorization") == "Bearer " + str(
            self.identity.grant_id
        ):
            return self.identity
        return None

    def require(self, grant, action, target):
        self.calls.append((grant, action, target))
        if grant != self.identity.grant_id:
            raise AgentError("permission_denied", "Controlled grant denied")
        if isinstance(target, ControlPlaneRequestTarget):
            if (
                target.workspace_id == self.workspace
                and target.principal_id == self.identity.principal_id
                and action
                in {"control_plane.query", "control_plane.command:SubmitTaskGoal"}
            ):
                return
        elif isinstance(target, PlanningCommandTarget):
            if (
                action == "planning.submit"
                and self.planning_selection is not None
                and target.workspace_id == self.workspace
                and target.principal_id == self.identity.principal_id
                and (
                    target.planning_profile_id,
                    target.planning_profile_version,
                    target.planner_agent_ref,
                )
                == self.planning_selection
                and target.planning_profile_version > 0
                and target.planning_request_id is None
                and target.root_task_id is None
                and target.context_refs == ()
                and target.proposal_content_hash is None
                and target.assignee_bindings == ()
            ):
                return
        elif isinstance(target, WorkspaceSetupRequest):
            if (
                action == "workspace.configure"
                and target.workspace_id == self.workspace
                and target.repository_directory == self.repository
            ):
                return
        elif isinstance(target, ExecutionWorkspace):
            if action == "workspace.execute" and target in self.workspaces:
                return
        elif isinstance(target, SecretBinding):
            if action == "secret.read" and target in self.secrets:
                return
        elif isinstance(target, ProfileRef):
            if target.workspace_id == self.workspace and action in {
                "profile.read",
                "run.start",
            }:
                return
        elif isinstance(target, UUID):
            if target == self.workspace and action in {
                "runtime.register",
                "runtime.list",
                "profile.create",
                "profile.list",
                "run.read",
                "run.stop",
            }:
                return
        elif (
            getattr(target, "workspace_id", None) == self.workspace
            and action == "runtime.read"
        ):
            return
        raise AgentError("permission_denied", "Controlled target denied")


def write_evidence(directory: Path | None, name: str, value: object):
    if directory is not None:
        directory.mkdir(exist_ok=True)
        path = directory / (name + "-" + uuid4().hex + ".json")
        path.write_text(
            json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8"
        )
        return path
    return None


@dataclass
class ProductLab:
    runtime: object
    native: ControlledHarness
    principal: ControlledPrincipal
    secrets: ControlledSecretStore
    directory: Path
    evidence: Path | None
    close_attempts: list[bool] = field(default_factory=list)

    def register_profile(self):
        runtime = self.runtime.agents.runtimes.register(
            self.principal.workspace,
            self.native.harness_id,
            sys.executable,
            max_active=2,
            max_sessions=2,
            grant_id=self.principal.identity.grant_id,
        )
        return self.runtime.agents.catalog.create_profile(
            self.principal.workspace,
            ProfileDraft(
                name="Owned controlled profile",
                description="Provider-free lifecycle fixture",
                runtime_ref=runtime.ref,
                instructions="Observe only the explicit controlled request.",
                resource_refs=(),
                model_id=None,
                thinking=None,
                max_active=2,
                environment={},
                secret_refs=(),
                native_settings={},
            ),
            grant_id=self.principal.identity.grant_id,
        )

    def request(self, profile):
        workspace = ExecutionWorkspace(
            directory=str(self.directory), writable=True, artifact_directory=None
        )
        self.principal.workspaces.add(workspace)
        return RunStart(
            operation_id=uuid4(),
            profile_ref=profile.ref,
            grant_id=self.principal.identity.grant_id,
            policy="bounded",
            instructions=(),
            text="Controlled lifecycle call",
            context=(),
            tools=(),
            workspace=workspace,
            required_capabilities=("prompt",),
        )

    def safe_close(self):
        self.native.release.set()
        self.native.observe_gone()
        complete = self.runtime.close(grace_seconds=10)
        self.close_attempts.append(complete)
        write_evidence(
            self.evidence,
            "lab-close",
            {
                "workspace_id": str(self.principal.workspace),
                "status": self.runtime.status(),
                "close_attempts": self.close_attempts,
                "native_calls": self.native.calls,
                "native_processes": 0,
            },
        )
        return complete


def build_lab(
    targets,
    tmp_path,
    evidence=None,
    *,
    workspace_id: UUID | None = None,
    knowledge_url: str | None = None,
    knowledge_token: SecretStr | None = None,
):
    by_component = {target.component: target for target in targets}
    workspace = uuid4() if workspace_id is None else workspace_id
    principal = ControlledPrincipal(
        workspace, "controlled-product-user", tmp_path / "repository"
    )
    native, secrets = ControlledHarness(), ControlledSecretStore()
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
        deployment_namespace=UUID("898937f8-0a89-51ea-bfee-563b8524f4ad"),
        workspace_id=workspace,
        local_principal_id=principal.identity.principal_id,
        workspace_root=tmp_path / "workspaces",
        artifact_root=tmp_path / "artifacts",
        agent_poll_interval_seconds=0.01,
        knowledge_base_url=knowledge_url,
        knowledge_token=knowledge_token,
    )
    runtime = build_product_runtime(
        settings,
        harnesses=(native,),
        request_authorizer=principal,
        product_authorizer=principal,
        secret_store=secrets,
    )
    return ProductLab(runtime, native, principal, secrets, tmp_path, evidence)
