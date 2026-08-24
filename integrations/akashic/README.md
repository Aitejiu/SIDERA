# SIDERA Akashic Task Bridge

`sidera-task-bridge` is an Akashic Plugin API v2 adapter. It lets the Akashic
Main Agent submit a structured brief to a local SIDERA Control Plane, inspect
planning and Task First state, and make explicit planning decisions. SIDERA
remains the owner of PlanningRequest, Proposal, Task, Subtask, WorkerItem, and
execution facts.

## Requirements

- Akashic host with Plugin API `2` and its normal `requirements.txt` installed.
- SIDERA Control Plane reachable on a loopback HTTP origin.
- A configured SIDERA Workspace and selectable Planning Profile.
- SIDERA Web reachable on a loopback origin for deep links.

The bridge does not implement remote authentication. Do not expose the
configured Control Plane or Web origins outside the local machine.

## Configuration

The plugin config is validated by `TaskBridgeConfig`:

```toml
workspace_id = "00000000-0000-0000-0000-000000000001"
planning_profile_id = "00000000-0000-0000-0000-000000000002"
planning_profile_version = 1
control_plane_url = "http://127.0.0.1:8000"
web_base_url = "http://127.0.0.1:4174"
actor_type = "integration"
actor_id = "akashic-task-bridge"
connect_timeout_seconds = 2.0
request_timeout_seconds = 15.0
notification_retry_base_seconds = 0.5
notification_retry_max_seconds = 15.0
notification_delivery_timeout_seconds = 15.0
notification_event_limit_bytes = 262144
```

Only `127.0.0.1`, `localhost`, and IPv6 loopback URLs are accepted. URL
userinfo, query, fragment, and non-origin Web paths are rejected. Workspace,
actor, profile, command, and source identity fields are injected by the
bridge; the Main Agent cannot provide them as tool arguments.

## Tools

| Tool | Purpose | Risk |
| --- | --- | --- |
| `sidera_submit_brief` | Submit goal, constraints, acceptance criteria, context references, and requested outputs | write |
| `sidera_get_planning_status` | Read planning status, blocking questions, proposal summary, and materialization | read-only |
| `sidera_answer_clarifications` | Submit explicit answers for the current clarification IDs | write |
| `sidera_request_proposal_revision` | Request a new immutable Proposal revision with feedback | write |
| `sidera_confirm_proposal` | Confirm one exact Proposal revision and content hash | write |
| `sidera_reject_proposal` | Reject one exact Proposal revision and record the reason | write |
| `sidera_get_task_status` | Read bounded Task First progress, counts, attention, next keys, and deep link | read-only |

All tools return a JSON `ToolEnvelope` with one of `ok`, `needs_user`,
`conflict`, `unavailable`, `result_unknown`, or `failed`. The bridge never
returns a complete Proposal, raw HTTP body, provider error, secret, or tool
context. A proposal confirmation is blocked unless the caller explicitly sets
`explicit_user_confirmation=true`; that flag is a local gate and is not sent
to the Control Plane.

## Persistence and recovery

The plugin stores only bridge-owned state under the host plugin data directory,
using the `sidera_task_bridge_state_v1` KV key. Before every write it stores
the exact normalized outbound request. Applied and rejected commands remove
their request body and keep only a bounded, non-sensitive audit. A transport
timeout yields `result_unknown`; the bridge queries the same command ID before
any retry and never creates a second business command automatically.

Private-channel routing is stored only as an opaque source reference and its
minimal host route. Web sessions use `source=sidera_web` and do not create a
private route. Plugin retirement does not delete or rewrite this state. The
plugin-data document is migrated explicitly from schema v1 to v2; migration
failure is fail-closed and never replaces the original document with an empty
state.

The activated plugin owns one managed
`sidera-task-bridge-supervisor` task. It first recovers unresolved commands and
only then starts the notification runner. Notifications are low-frequency
facts only: blocking clarification, Proposal ready, materialization, needs
user, blocked, failed, milestone completion, and root completion. Worker steps,
tool traces, runtime names, heartbeats, and token usage are never pushed
proactively.

Private notices return to the original `akashic_private` route recorded when
the PlanningRequest was created. The route owner does not change when another
入口 later reads or edits the resource. Delivery is at-least-once: pending
state is written before `message_push`, only the exact `消息已发送` receipt
settles it, and a crash after delivery may cause one bounded duplicate. Partial
receipts, errors, timeouts, and temporary route failures remain pending;
permanently missing routes become bounded `route_missing` audits and do not
block later cursor positions. Cursor expiry or stream disconnect re-queries
current Planning/Task facts and does not replay historical milestone bursts.

Web-origin resources remain in the SIDERA Web attention/inbox surfaces but do
not trigger private `message_push`. Full WorkerItem and Execution trace stays
available from the Web control-room entry point.

## Development and release

The canonical source is `integrations/akashic/`. The Akashic snapshot under
`akashic-agent/` is read-only. Run the local checks with the project environment
and the host source on `PYTHONPATH`:

```bash
PYTHONPATH="$PWD/integrations/akashic:$PWD/akashic-agent" \
  uv run --project integrations/akashic pytest integrations/akashic/tests -q
uv run --project integrations/akashic ruff check integrations/akashic
uv run --project integrations/akashic pyright -p integrations/akashic
```

The installed artifact must have `plugin.py` at its checkout root. Generate a
versioned subtree ref from a committed SIDERA revision:

```bash
git subtree split --prefix=integrations/akashic \
  -b release/akashic-task-bridge-r5-2-1
```

Install that split ref through Akashic's formal installer, using an explicit
workspace and config path:

```bash
cd akashic-agent
uv run python main.py plugin-install \
  --source /path/to/sidera \
  --ref release/akashic-task-bridge-r5-2-1 \
  --marketplace sidera \
  --workspace /path/to/akashic-workspace \
  --config /path/to/akashic-config.toml \
  --json
```

Do not install the canonical nested directory directly, edit plugin cache or
manifest files, or manually copy plugin data. Candidate failures are fixed in
the canonical source, committed, split again under a new explicit ref, and
reinstalled through the formal lifecycle.
