"""Run the shared Adapter v1 handoff contract against local transports."""

from __future__ import annotations

import stat
import textwrap
from pathlib import Path

import pytest
from contract.handoff_adapter_conformance import (
    HandoffAdapterFactory,
    build_handoff_fixture,
    run_source_role,
    run_target_role,
)

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter


class _FakeAdapterFactory:
    name = "fake"

    def create(self, root: Path):
        return build_handoff_fixture(
            None,
            root,
            adapter_factory=lambda source_context: FakeWorkerAdapter(
                handoff_context=(
                    source_context.metadata.package_id,
                    source_context.metadata.package_hash,
                    source_context.metadata.context_version or 0,
                )
            ),
        )


class _OpenCodeContractFactory:
    name = "opencode-acp-contract"

    def create(self, root: Path):
        executable = _contract_acp_executable(root)
        adapter = OpenCodeACPAdapter(
            executable=str(executable),
            workspace_root=root,
            request_timeout_seconds=2,
            handoff_request_timeout_seconds=2,
            stop_grace_seconds=0.2,
        )
        return build_handoff_fixture(
            adapter,
            root,
            probe_config={"cwd": str(root)},
        )


@pytest.mark.parametrize(
    "factory",
    (_FakeAdapterFactory(), _OpenCodeContractFactory()),
    ids=("fake", "opencode-acp-contract"),
)
def test_source_role_conformance(
    factory: HandoffAdapterFactory, tmp_path: Path
) -> None:
    run_source_role(factory, tmp_path)


@pytest.mark.parametrize(
    "factory",
    (_FakeAdapterFactory(), _OpenCodeContractFactory()),
    ids=("fake", "opencode-acp-contract"),
)
def test_target_role_conformance(
    factory: HandoffAdapterFactory, tmp_path: Path
) -> None:
    run_target_role(factory, tmp_path)


def _contract_acp_executable(root: Path) -> Path:
    executable = root / "contract-opencode-acp"
    executable.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import os
            import sys

            session_id = f"contract-session-{os.getpid()}"

            def write(message):
                sys.stdout.write(json.dumps(message) + "\\n")
                sys.stdout.flush()

            for raw in sys.stdin:
                request = json.loads(raw)
                method = request.get("method")
                request_id = request.get("id")
                if method == "initialize":
                    write({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "protocolVersion": 1,
                            "agentCapabilities": {"loadSession": False},
                            "agentInfo": {
                                "name": "contract-opencode",
                                "version": "handoff-contract",
                            },
                        },
                    })
                elif method == "session/new":
                    write({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "sessionId": session_id,
                            "configOptions": [{
                                "id": "mode",
                                "name": "Session Mode",
                                "category": "mode",
                                "type": "select",
                                "currentValue": "build",
                                "options": [
                                    {"value": "build", "name": "build"},
                                    {"value": "plan", "name": "plan"},
                                ],
                            }],
                        },
                    })
                elif method == "session/set_mode":
                    write({"jsonrpc": "2.0", "id": request_id, "result": {}})
                elif method == "session/prompt":
                    write({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {
                                    "type": "text",
                                    "text": "contract turn complete",
                                },
                            },
                        },
                    })
                    write({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"stopReason": "end_turn"},
                    })
                elif method == "session/cancel":
                    continue
            """
        ).strip()
        + "\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable
