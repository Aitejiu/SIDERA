import importlib
from dataclasses import replace
from threading import Lock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sidera_agents.contracts import RunState
from sidera_agents.errors import AgentError
from sidera_control_plane.api.schemas import CommandStatusResponse
from sidera_control_plane_client import (
    ActorRef,
    ControlPlaneClient,
    ControlPlaneClientConfig,
)

from tests.unit.test_product_grants import grant_fixture


def transport_module():
    assert importlib.util.find_spec("sidera_product.task_tools") is not None, (
        "N18 requires original-command transport"
    )
    return importlib.import_module("sidera_product.task_tools.transport")


@pytest.mark.anyio
async def test_public_client_accepts_actual_cp_pending_serializer():
    workspace, command = uuid4(), uuid4()
    actual = CommandStatusResponse(command_id=command, status="pending")
    body = actual.model_dump(mode="json")
    assert len(body) == 10
    client = ControlPlaneClient(
        ControlPlaneClientConfig(
            control_plane_url="http://127.0.0.1:18080",
            workspace_id=workspace,
            actor=ActorRef(type="agent", id="owned-user"),
        ),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    try:
        status = await client.get_command_status(command)
        assert status.command_id == command and status.status == "pending"
        assert status.command_type is status.payload is status.payload_sha256 is None
    finally:
        await client.close()


@pytest.mark.anyio
async def test_receipt_concurrent_loser_cannot_overwrite_committed_original_with_stale_unknown(
    settings_values, tmp_path
):
    import anyio

    lab, token, store, receipts, module = receipt_fixture(settings_values, tmp_path)
    post_started, release_post, loser_lookup, release_loser, first_done = (
        anyio.Event() for _ in range(5)
    )
    requests, results = [], {}
    logical, root = uuid4(), uuid4()
    committed = False

    async def respond(request):
        nonlocal committed
        requests.append(request.method)
        row = next(iter(store.rows.values()))
        if request.method == "POST":
            post_started.set()
            await release_post.wait()
            committed = True
            raise httpx.ReadError("Controlled loss after the original effect")
        if not committed:
            pending = CommandStatusResponse(
                command_id=row.command_id, status="pending"
            ).model_dump(mode="json")
            loser_lookup.set()
            await release_loser.wait()
            return httpx.Response(200, json=pending)
        return httpx.Response(
            200,
            json=actual_applied_status(row.command_id, root).model_dump(mode="json"),
        )

    async def prepare(verified, command_id):
        return {"command_id": str(command_id)}

    client = command_client(respond, lab.profile.ref.workspace_id)

    async def call(label):
        results[label] = await receipts.execute(
            token,
            "sidera_submit_goal",
            logical,
            {"objective": "One concurrent intent"},
            prepare=prepare,
            transport=module.OriginalCommandTransport(client),
        )
        if label == "first":
            first_done.set()

    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(call, "first")
                await post_started.wait()
                group.start_soon(call, "loser")
                await loser_lookup.wait()
                release_post.set()
                await first_done.wait()
                release_loser.set()
        assert (
            results["first"] == results["loser"]
            and results["first"].outcome == "applied"
        )
        assert results["first"].root_task_id == root and requests.count("POST") == 1
        assert requests.count("GET") == 2 and len(store.rows) == 1
        assert next(iter(store.rows.values())).outcome == "applied"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_receipt_unknown_reconnect_queries_original_without_automatic_execute(
    settings_values, tmp_path
):
    lab, token, store, receipts, module = receipt_fixture(settings_values, tmp_path)
    logical, root = uuid4(), uuid4()
    requests, visible = [], False

    def respond(request):
        requests.append(request.method)
        row = next(iter(store.rows.values()))
        if request.method == "POST":
            raise httpx.ReadError("Controlled unobserved original reply")
        if not visible:
            return httpx.Response(
                404,
                json={
                    "error": {
                        "code": "not_found",
                        "message": "Not currently observed",
                        "details": {},
                    }
                },
            )
        return httpx.Response(
            200,
            json=actual_applied_status(row.command_id, root).model_dump(mode="json"),
        )

    async def prepare(verified, command_id):
        return {"command_id": str(command_id)}

    client = command_client(respond, lab.profile.ref.workspace_id)
    try:
        first = await receipts.execute(
            token,
            "sidera_submit_goal",
            logical,
            {"objective": "Retain UNKNOWN"},
            prepare=prepare,
            transport=module.OriginalCommandTransport(client),
        )
        assert first.outcome == "unknown" and first.root_task_id is None
        reopened = type(receipts)(lab.build(), store)
        second = await reopened.execute(
            token,
            "sidera_submit_goal",
            logical,
            {"objective": "Retain UNKNOWN"},
            prepare=prepare,
            transport=module.OriginalCommandTransport(client),
        )
        assert second == first and requests == ["POST", "GET", "GET"]
        visible = True
        settled = await reopened.execute(
            token,
            "sidera_submit_goal",
            logical,
            {"objective": "Retain UNKNOWN"},
            prepare=prepare,
            transport=module.OriginalCommandTransport(client),
        )
        assert (
            settled.outcome == "applied"
            and settled.command_id == first.command_id
            and settled.root_task_id == root
        )
        assert requests == ["POST", "GET", "GET", "GET"] and len(store.rows) == 1
    finally:
        await client.close()


def command_client(transport, workspace):
    return ControlPlaneClient(
        ControlPlaneClientConfig(
            control_plane_url="http://127.0.0.1:18080",
            workspace_id=workspace,
            actor=ActorRef(type="agent", id="owned-user"),
        ),
        transport=httpx.MockTransport(transport),
    )


def actual_applied_status(command, root):
    return CommandStatusResponse(
        command_id=command,
        status="applied",
        command_type="SubmitTaskGoal",
        target_type="workspace",
        payload_sha256="a" * 64,
        payload={"objective": "Owned logical intent"},
        result={
            "command_id": str(command),
            "aggregate_id": str(root),
            "aggregate_version": 1,
            "event_ids": [],
            "data": {"root_task_id": str(root), "status": "pending_planning"},
        },
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reply", ["malformed", "native_403", "malformed_503", "lost", "mismatched"]
)
async def test_uncertain_dispatch_queries_original_command_without_resend(reply):
    module = transport_module()
    workspace, command, root = uuid4(), uuid4(), uuid4()
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200, json=actual_applied_status(command, root).model_dump(mode="json")
            )
        if reply == "lost":
            raise httpx.ReadError("Controlled reply loss")
        if reply == "native_403":
            return httpx.Response(403, json={"detail": {"code": "permission_denied"}})
        if reply == "mismatched":
            return httpx.Response(
                201,
                json=actual_applied_status(uuid4(), root).result.model_dump(
                    mode="json"
                ),
            )
        return httpx.Response(
            503 if reply == "malformed_503" else 200, json={"malformed": True}
        )

    client = command_client(respond, workspace)
    try:
        result = await module.OriginalCommandTransport(client).dispatch({}, command)
        assert result.outcome == "applied" and result.command_id == command
        assert result.root_task_id == root and result.error_code is None
        assert requests == [
            ("POST", "/api/v1/commands"),
            ("GET", f"/api/v1/workspaces/{workspace}/commands/{command}"),
        ]
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "lookup",
    [
        "pending",
        "unavailable",
        "malformed",
        "wrong_id",
        "missing_result",
        "wrong_result",
    ],
)
async def test_uncertain_original_lookup_stays_unknown_without_foreign_links(lookup):
    module = transport_module()
    workspace, command, root = uuid4(), uuid4(), uuid4()
    requests = []

    def respond(request):
        requests.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"malformed": True})
        if lookup == "unavailable":
            raise httpx.ConnectError("Owned unavailable read")
        if lookup == "malformed":
            return httpx.Response(200, json={"malformed": True})
        body = actual_applied_status(command, root).model_dump(mode="json")
        if lookup == "pending":
            body.update(status="pending", result=None)
        elif lookup == "wrong_id":
            body["command_id"] = str(uuid4())
        elif lookup == "missing_result":
            body["result"] = None
        elif lookup == "wrong_result":
            body["result"]["command_id"] = str(uuid4())
        return httpx.Response(200, json=body)

    client = command_client(respond, workspace)
    try:
        result = await module.OriginalCommandTransport(client).dispatch({}, command)
        assert result.outcome == "unknown" and result.command_id == command
        assert result.root_task_id is result.planning_request_id is None
        assert requests == ["POST", "GET"]
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("query", [False, True])
async def test_valid_original_business_rejection_remains_rejected(query):
    module = transport_module()
    command, workspace = uuid4(), uuid4()

    def respond(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json=CommandStatusResponse(
                    command_id=command, status="rejected", error_code="version_conflict"
                ).model_dump(mode="json"),
            )
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "version_conflict",
                    "message": "Conflict",
                    "details": {},
                }
            },
        )

    client = command_client(respond, workspace)
    try:
        transport = module.OriginalCommandTransport(client)
        result = (
            await transport.reconcile(command)
            if query
            else await transport.dispatch({}, command)
        )
        assert result.outcome == "rejected" and result.command_id == command
        assert result.error_code == "version_conflict"
    finally:
        await client.close()


class MemoryReceiptStore:
    def __init__(self):
        self.rows = {}
        self.lock = Lock()

    def find_receipt(
        self, *, grant_id, tool_name=None, logical_request_id=None, command_id=None
    ):
        return next(
            (
                row
                for row in self.rows.values()
                if row.grant_id == grant_id
                and (
                    row.command_id == command_id
                    if command_id is not None
                    else row.tool_name == tool_name
                    and row.logical_request_id == logical_request_id
                )
            ),
            None,
        )

    def save_receipt(self, row):
        with self.lock:
            existing = self.find_receipt(
                grant_id=row.grant_id,
                tool_name=row.tool_name,
                logical_request_id=row.logical_request_id,
            )
            if existing is not None:
                return existing, False
            self.rows[row.receipt_id] = row
            return row, True

    def settle_receipt(self, receipt_id, *, outcome, result, now):
        with self.lock:
            row = self.rows[receipt_id]
            if row.outcome not in {"applied", "rejected"}:
                row = replace(row, outcome=outcome, result=result, updated_at=now)
                self.rows[receipt_id] = row
            return row


def receipt_fixture(settings_values, tmp_path):
    lab = grant_fixture(settings_values, tmp_path, state=RunState.RUNNING)
    connection = lab.grants.resolve(lab.owner, lab.grants.connection_id)
    token = SecretStr(
        connection.headers["Authorization"].get_secret_value().removeprefix("Bearer ")
    )
    store = MemoryReceiptStore()
    module = importlib.import_module("sidera_product.task_tools.transport")
    assert importlib.util.find_spec("sidera_product.task_tools.receipts") is not None, (
        "N18 requires durable logical receipts"
    )
    receipts_module = importlib.import_module("sidera_product.task_tools.receipts")
    receipts = receipts_module.ToolReceipts(lab.grants, store)
    return lab, token, store, receipts, module


@pytest.mark.anyio
async def test_logical_receipt_dispatches_once_and_reopens_original_intent(
    settings_values, tmp_path
):
    lab, token, store, receipts, module = receipt_fixture(settings_values, tmp_path)
    requests, prepared = [], []
    root, logical_id = uuid4(), uuid4()

    def respond(request):
        import json

        body = json.loads(request.content)
        requests.append(body)
        assert len(store.rows) == 1
        row = next(iter(store.rows.values()))
        assert (
            row.outcome == "dispatching" and str(row.command_id) == body["command_id"]
        )
        return httpx.Response(
            201,
            json=actual_applied_status(row.command_id, root).result.model_dump(
                mode="json"
            ),
        )

    async def prepare(verified, command_id):
        prepared.append(command_id)
        assert verified.run.active_response_id == lab.run[0].active_response_id
        return {
            "command_id": str(command_id),
            "selected_profile": lab.profile.ref.model_dump(mode="json"),
        }

    client = command_client(respond, lab.profile.ref.workspace_id)
    try:
        first = await receipts.execute(
            token,
            "sidera_submit_goal",
            logical_id,
            {"objective": "Original intent"},
            prepare=prepare,
            transport=module.OriginalCommandTransport(client),
        )
        reopened = type(receipts)(lab.grants, store)
        second = await reopened.execute(
            token,
            "sidera_submit_goal",
            logical_id,
            {"objective": "Original intent"},
            prepare=prepare,
            transport=module.OriginalCommandTransport(client),
        )
        assert (
            first == second
            and first.outcome == "applied"
            and first.root_task_id == root
        )
        row = next(iter(store.rows.values()))
        assert (
            row.run_id == lab.run[0].run_id
            and row.response_id == lab.run[0].active_response_id
        )
        assert row.intent["selected_profile"] == lab.profile.ref.model_dump(mode="json")
        assert len(requests) == len(prepared) == len(store.rows) == 1
        assert token.get_secret_value() not in repr(row)
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("variant", ["payload", "response", "revoked", "role"])
async def test_logical_receipt_conflict_or_stale_authority_denies_before_disclosure(
    settings_values, tmp_path, variant
):
    lab, token, store, receipts, module = receipt_fixture(settings_values, tmp_path)
    requests, logical_id = [], uuid4()

    def respond(request):
        requests.append(request.method)
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "version_conflict",
                    "message": "Conflict",
                    "details": {},
                }
            },
        )

    async def prepare(verified, command_id):
        return {"command_id": str(command_id)}

    client = command_client(respond, lab.profile.ref.workspace_id)
    transport = module.OriginalCommandTransport(client)
    try:
        first = await receipts.execute(
            token,
            "sidera_submit_goal",
            logical_id,
            {"objective": "Original"},
            prepare=prepare,
            transport=transport,
        )
        assert first.outcome == "rejected"
        payload, tool = {"objective": "Original"}, "sidera_submit_goal"
        if variant == "payload":
            payload = {"objective": "Changed"}
        elif variant == "response":
            lab.run[0] = lab.run[0].model_copy(update={"active_response_id": uuid4()})
        elif variant == "revoked":
            lab.grants.revoke(lab.owner)
        else:
            tool = "sidera_report_progress"
        before = dict(store.rows)
        with pytest.raises(AgentError):
            await receipts.execute(
                token, tool, logical_id, payload, prepare=prepare, transport=transport
            )
        assert requests == ["POST"] and store.rows == before
    finally:
        await client.close()
