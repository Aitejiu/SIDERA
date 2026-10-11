import argparse
import json
import os
import subprocess
import sys
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn
from uuid import UUID

import httpx
import uvicorn
from pydantic import JsonValue, TypeAdapter, ValidationError
from sidera_agents.contracts import ProfileRef
from sidera_control_plane.composition.native_runtime import (
    ControlPlaneIdentity,
    WorkspaceSetupRequest,
)
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.planning_profiles import PlanningBudgetPolicy
from sidera_control_plane.domain.result_integration import CheckSpecV1

from .app import create_app
from .auth.local_session import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    LocalSessionError,
    LocalSessions,
    read_bootstrap_file,
    write_bootstrap_file,
)
from .composition import build_product_runtime
from .config import ProductSettings
from .lifecycle import ProductRuntime, credential_alembic_config_path
from .postgres import PostgresProductStore
from .postgres.authority_store import PostgresAuthorityStore
from .postgres.store import ProductStoreError


class ProductCLIError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.exit(2, json.dumps({"error": {"code": "invalid_arguments"}}) + "\n")


def parse_planner_profile(value: str | None, workspace_id: UUID) -> ProfileRef | None:
    if value is None:
        return None
    try:
        ref = ProfileRef.model_validate(json.loads(value))
        if ref.workspace_id != workspace_id:
            raise ValueError("Workspace mismatch")
        return ref
    except (ValueError, ValidationError):
        raise ProductCLIError("invalid_planner_profile") from None


def parse_verification_checks(value: str | None) -> tuple[CheckSpecV1, ...] | None:
    if value is None:
        return None
    try:
        parsed = TypeAdapter(list[dict[str, JsonValue]]).validate_json(value)
        if not parsed:
            raise ProductCLIError("verification_policy_required")
        return tuple(CheckSpecV1.from_mapping(item) for item in parsed)
    except (ValueError, DomainError):
        raise ProductCLIError("invalid_verification_checks") from None


def migrate_credentials(settings: ProductSettings) -> None:
    config_path = credential_alembic_config_path()
    environment = {
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
        "LANG": "C",
        "PYTHONIOENCODING": "utf-8",
        "SIDERA_CONFIGURATION_DATABASE_URL": settings.credential_database_url.get_secret_value(),
    }
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(config_path),
                "upgrade",
                "head",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=settings.migration_timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ProductCLIError("credential_migration_failed") from None
    if completed.returncode != 0:
        raise ProductCLIError("credential_migration_failed")


def migrate_runtime(runtime: ProductRuntime) -> None:
    runtime.product_store.migrate()
    runtime.agents.migrate()
    runtime.control_plane.migrate()
    migrate_credentials(runtime.settings)


def parser() -> SafeArgumentParser:
    root = SafeArgumentParser(prog="sidera")
    commands = root.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port")
    commands.add_parser("open")
    commands.add_parser("migrate")
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--json", action="store_true")
    session = commands.add_parser("session")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    bootstrap = session_commands.add_parser("bootstrap")
    bootstrap.add_argument("--output", type=Path)
    workspace = commands.add_parser("workspace")
    workspace_commands = workspace.add_subparsers(
        dest="workspace_command", required=True
    )
    configure = workspace_commands.add_parser("configure")
    configure.add_argument("--workspace", required=True)
    configure.add_argument("--repository", required=True)
    configure.add_argument("--planner-profile")
    configure.add_argument("--verification-checks")
    configure.add_argument("--expected-version", default="0")
    configure.add_argument("--bootstrap-file", type=Path)
    return root


def bootstrap_local_session(settings: ProductSettings, *, output: Path | None) -> None:
    store = PostgresProductStore(settings.product_database_url.get_secret_value())
    try:
        if not store.readiness():
            raise ProductCLIError("schema_not_ready")
        sessions = LocalSessions(settings, PostgresAuthorityStore(store.engine))
        issue = sessions.create_bootstrap()
        location = write_bootstrap_file(issue, output=output)
        print(
            json.dumps(
                {"output": str(location), "expires_at": issue.expires_at.isoformat()}
            )
        )
    finally:
        store.close()


def configure_workspace(
    runtime: ProductRuntime,
    *,
    identity: ControlPlaneIdentity | None,
    workspace: str,
    repository: str,
    planner_profile: str | None,
    verification_checks: str | None,
    expected_version: str,
) -> None:
    if (
        identity is None
        or identity.actor_type != "user"
        or identity.principal_id != runtime.settings.local_principal_id
        or runtime.request_authorizer.identity_for_grant(identity.grant_id) != identity
    ):
        raise ProductCLIError("local_session_required")
    request = workspace_setup_request(
        runtime.settings,
        workspace=workspace,
        repository=repository,
        planner_profile=planner_profile,
        verification_checks=verification_checks,
        expected_version=expected_version,
    )
    receipt = runtime.control_plane.configure_workspace(
        request, grant_id=identity.grant_id
    )
    print(
        json.dumps(
            {
                "workspace_id": str(receipt.workspace_id),
                "version": receipt.version,
                "status": receipt.status,
            }
        )
    )


def workspace_setup_request(
    settings: ProductSettings,
    *,
    workspace: str,
    repository: str,
    planner_profile: str | None,
    verification_checks: str | None,
    expected_version: str,
) -> WorkspaceSetupRequest:
    try:
        workspace_id = UUID(workspace)
        version = int(expected_version)
        directory = Path(repository)
        if (
            workspace_id != settings.workspace_id
            or version < 0
            or not directory.is_absolute()
            or ".." in directory.parts
        ):
            raise ValueError("Invalid setup selection")
    except ValueError:
        raise ProductCLIError("invalid_workspace_setup") from None
    return WorkspaceSetupRequest(
        workspace_id=workspace_id,
        repository_directory=directory,
        expected_version=version,
        planning_budget=PlanningBudgetPolicy(
            max_context_bytes=1_048_576,
            max_tokens=65_536,
            max_cost_microunits=10_000_000,
            max_runtime_seconds=3600,
            max_revisions=3,
            max_nodes=64,
            max_concurrency=3,
        ),
        planner_agent_ref=parse_planner_profile(planner_profile, workspace_id),
        verification_checks=parse_verification_checks(verification_checks),
    )


def _require_http_success(reply: httpx.Response) -> dict[str, JsonValue]:
    try:
        body = TypeAdapter(dict[str, JsonValue]).validate_json(reply.content)
    except ValidationError:
        raise ProductCLIError("product_operation_failed") from None
    if reply.is_success:
        return body
    detail = body.get("detail")
    code = detail.get("code") if isinstance(detail, dict) else None
    if isinstance(code, str) and code in {
        "bootstrap_consumed",
        "bootstrap_expired",
        "bootstrap_invalid",
        "local_session_required",
        "csrf_required",
        "origin_denied",
        "host_denied",
        "permission_denied",
        "schema_not_ready",
        "setup_required",
        "validation_error",
        "revision_conflict",
        "configuration_mismatch",
    }:
        raise ProductCLIError(code)
    raise ProductCLIError("product_operation_failed")


def configure_live_workspace(
    settings: ProductSettings, request: WorkspaceSetupRequest, *, bootstrap_file: Path
) -> None:
    bootstrap = read_bootstrap_file(bootstrap_file)
    payload: dict[str, JsonValue] = TypeAdapter(dict[str, JsonValue]).validate_python(
        {
            "repository_directory": str(request.repository_directory),
            "expected_version": request.expected_version,
            "planner_agent_ref": None
            if request.planner_agent_ref is None
            else request.planner_agent_ref.model_dump(mode="json"),
            "planning_budget": asdict(request.planning_budget),
            "verification_checks": None
            if request.verification_checks is None
            else [check.to_mapping() for check in request.verification_checks],
        }
    )
    with httpx.Client(
        base_url=settings.local_url,
        headers={"Origin": settings.web_origin},
        trust_env=False,
        timeout=30.0,
        follow_redirects=False,
    ) as client:
        _require_http_success(
            client.post(
                "/product/v1/session/bootstrap",
                json={"token": bootstrap.get_secret_value()},
            )
        )
        csrf = client.cookies.get(CSRF_COOKIE)
        if not csrf or not client.cookies.get(SESSION_COOKIE):
            raise ProductCLIError("local_session_required")
        reply = _require_http_success(
            client.post(
                f"/product/v1/workspaces/{request.workspace_id}/setup",
                json=payload,
                headers={CSRF_HEADER: csrf},
            )
        )
    version, status = reply.get("version"), reply.get("status")
    if (
        reply.get("workspace_id") != str(request.workspace_id)
        or type(version) is not int
        or version < 1
        or status not in {"created", "updated", "unchanged"}
    ):
        raise ProductCLIError("product_operation_failed")
    print(
        json.dumps(
            {
                "workspace_id": str(request.workspace_id),
                "version": version,
                "status": status,
            }
        )
    )


def main(
    argv: list[str] | None = None,
    *,
    settings: ProductSettings | None = None,
    runtime: ProductRuntime | None = None,
    trusted_identity: ControlPlaneIdentity | None = None,
) -> int:
    arguments = parser().parse_args(argv)
    owner = runtime
    result = 0
    try:
        selected = settings or (
            runtime.settings
            if runtime is not None
            else ProductSettings.model_validate({})
        )
        if arguments.command == "open":
            if not webbrowser.open(
                f"{selected.web_origin}/workspaces/{selected.workspace_id}/tasks"
            ):
                raise ProductCLIError("browser_unavailable")
            return 0
        if arguments.command == "session":
            bootstrap_local_session(selected, output=arguments.output)
            return 0
        if arguments.command == "workspace" and runtime is None:
            if arguments.bootstrap_file is None:
                raise ProductCLIError("local_session_required")
            request = workspace_setup_request(
                selected,
                workspace=arguments.workspace,
                repository=arguments.repository,
                planner_profile=arguments.planner_profile,
                verification_checks=arguments.verification_checks,
                expected_version=arguments.expected_version,
            )
            configure_live_workspace(
                selected, request, bootstrap_file=arguments.bootstrap_file
            )
            return 0
        if arguments.command == "serve":
            values = selected.model_dump()
            if arguments.host is not None:
                values["bind_host"] = arguments.host
            if arguments.port is not None:
                values["port"] = arguments.port
            selected = ProductSettings.model_validate(values)
            uvicorn.run(
                create_app(selected),
                host=selected.bind_host,
                port=selected.port,
                workers=1,
                reload=False,
                timeout_graceful_shutdown=selected.close_grace_seconds,
            )
            return 0
        owner = runtime or build_product_runtime(selected)
        if arguments.command == "migrate":
            migrate_runtime(owner)
            print(json.dumps({"status": "migrated"}))
        elif arguments.command == "preflight":
            components = owner.inspect_readiness()
            print(
                json.dumps(
                    {
                        "storage_ready": all(components.values()),
                        "components": components,
                        "status": owner.status(),
                    }
                )
            )
            result = 0 if all(components.values()) else 1
        elif arguments.command == "workspace":
            configure_workspace(
                owner,
                identity=trusted_identity,
                workspace=arguments.workspace,
                repository=arguments.repository,
                planner_profile=arguments.planner_profile,
                verification_checks=arguments.verification_checks,
                expected_version=arguments.expected_version,
            )
    except ProductCLIError as error:
        print(json.dumps({"error": {"code": error.code}}), file=sys.stderr)
        result = 1
    except (LocalSessionError, ProductStoreError) as error:
        print(json.dumps({"error": {"code": error.code}}), file=sys.stderr)
        result = 1
    except ValidationError:
        print(
            json.dumps({"error": {"code": "configuration_required"}}), file=sys.stderr
        )
        result = 2
    except Exception:  # noqa: BLE001 - CLI never relays raw driver/host/native exception strings
        print(
            json.dumps({"error": {"code": "product_operation_failed"}}), file=sys.stderr
        )
        result = 1
    finally:
        if owner is not None and runtime is None:
            try:
                complete = owner.close() is True
            except Exception:  # noqa: BLE001 - unconfirmed owned cleanup is a safe nonzero outcome
                complete = False
            if not complete:
                print(
                    json.dumps({"error": {"code": "product_cleanup_pending"}}),
                    file=sys.stderr,
                )
                result = 1
    return result
