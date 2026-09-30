# SIDERA Akashic Task Bridge

`sidera-task-bridge` is an Akashic Plugin API v2 adapter. It lets the Akashic
Main Agent submit a structured brief to a local SIDERA Control Plane, inspect
planning and Task First state, and make explicit planning decisions. SIDERA
remains the owner of PlanningRequest, Proposal, Task, Subtask, WorkerItem, and
execution facts.

For a provider-free local preview, start with the [quick start](../../docs/deployment/initial-demo.md).
The [API guide](../../docs/api/README.md) documents public endpoints and command
semantics. The sections below cover this adapter's configuration and installation.

## Requirements

- Akashic host with Plugin API `2` and its normal `requirements.txt` installed.
- SIDERA Control Plane reachable on a loopback HTTP origin.
- A configured SIDERA Workspace and selectable Planning Profile.
- SIDERA Web reachable on a loopback origin for deep links.

## Public API client dependency

The bridge uses the neutral `sidera-control-plane-client` package to call the
Control Plane's public HTTP API. In this monorepo, `integrations/akashic/pyproject.toml`
pins the client version and `tool.uv.sources` resolves it from
`../../packages/control-plane-client` for local development.

A wheel or subtree split of `integrations/akashic/` does not contain the client
package. A distributed bridge artifact must therefore include the matching
client wheel or depend on its fixed published version. An older remote split
plugin must not be assumed to include this new dependency.

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
knowledge_base_url = "http://127.0.0.1:8020"
knowledge_timeout_seconds = 5.0
actor_type = "integration"
actor_id = "akashic-task-bridge"
# Optional: both fields are required to enable Main Agent governance writes.
governance_actor_type = "local"
governance_actor_id = "aitejiu"
connect_timeout_seconds = 2.0
request_timeout_seconds = 15.0
notification_retry_base_seconds = 0.5
notification_retry_max_seconds = 15.0
notification_delivery_timeout_seconds = 15.0
notification_mode = "legacy"
# Required when notification_mode = "gateway".
notification_channel_module_instance_id = "00000000-0000-0000-0000-000000000070"
notification_gateway_contract_versions = [1]
notification_gateway_batch_size = 50
notification_gateway_lease_seconds = 30
notification_gateway_poll_seconds = 1.0
notification_event_limit_bytes = 262144
```

The Knowledge token is never accepted in this TOML file. Provide it only to
the Main Agent process environment:

```bash
export SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN="replace-me"
```

`SIDERA_KNOWLEDGE_BASE_URL` and `SIDERA_KNOWLEDGE_TIMEOUT_SECONDS` may provide
defaults when the corresponding plugin fields are omitted. Only
`127.0.0.1`, `localhost`, and IPv6 loopback URLs are accepted by default;
`knowledge_allow_remote = true` is an explicit opt-in for a separately secured
deployment. URL userinfo, query, fragment, and non-origin Web paths are
rejected. Workspace, actor, profile, command, and source identity fields are
injected by the bridge; the Main Agent cannot provide them as tool arguments.
Governance writes are opt-in: set both `governance_actor_type` and
`governance_actor_id` to the exact `local_actor_type` and `local_actor_id` used
by the Control Plane. The pair is never used for planning commands. If either
field is omitted, governance writes remain unavailable. Deployment readiness
must compare the pair before enabling the feature; a mismatch is fail-closed
at the Control Plane command boundary.
The Knowledge token is separate from the Control Plane token and every
OpenCode/provider key. If it is absent, the planning/task tools remain available
and the three knowledge tools return `unavailable` without making an HTTP request.

The bridge currently registers twenty-six tools: eight `always_on` tools,
including one structured brief-entry tool and seven status/reference reads, plus
seventeen explicit decision, notification, candidate, and governance tools. Knowledge search and
page reads remain bounded reference reads; candidate submission is always
review-gated and never publishes a revision. Governance writes are fixed to one
WorkerItem or one release Proposal/Hold at a time and never expose actor or
transport arguments.

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
| `sidera_get_collaboration_governance` | Read bounded release-governance state, path, actions, and safe history for one WorkerItem | read-only |
| `sidera_get_worker_result_inputs` | Read bounded result-input groups, selected sources, blockers, delivery, and recovery actions for one WorkerItem | read-only |
| `sidera_pause_collaboration_release` | Pause automatic release for one exact WorkerItem and plan generation | write |
| `sidera_approve_collaboration_release` | Approve one exact pending release Proposal after explicit user confirmation | write |
| `sidera_reject_collaboration_release` | Reject one exact pending release Proposal | write |
| `sidera_resume_automatic_collaboration` | Resume automatic release for one exact active Hold | write |
| `sidera_search_knowledge` | Search bounded project knowledge summaries and exact page references | read-only |
| `sidera_get_knowledge_page` | Read one current or exact project knowledge page revision | read-only |
| `sidera_submit_knowledge_candidate` | Submit a full knowledge snapshot for user review | write |

All tools return a JSON `ToolEnvelope` with one of `ok`, `needs_user`,
`conflict`, `unavailable`, `result_unknown`, or `failed`. Knowledge search
returns summaries and exact refs only. Knowledge page reads return bounded
reference text marked as project knowledge, not system instructions. Candidate
submission returns `needs_user` with a Candidate deep link; it never claims a
Knowledge Revision was published. Review, accept, archive, and materialize
remain Web/owner operations and are not Main Agent tools. The bridge never
returns a complete Proposal, raw HTTP body, provider error, secret, or tool
context. A proposal confirmation is blocked unless the caller explicitly sets
`explicit_user_confirmation=true`; that flag is a local gate and is not sent
to the Control Plane. Governance approval uses the same pattern. Every
governance write first reads the current WorkerItem/Proposal view, checks the
resource version and current lineage, and verifies the private task source
binding or the `sidera_web_chat` source marker. Stale, ambiguous, or foreign
source requests return a bounded envelope without dispatching a command.

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
only then starts exactly one notification runner. `legacy` keeps the local
Projection SSE evaluator and pending queue. `gateway` pulls the Control Plane
`notification.delivery.v1` envelope, resolves the opaque `source_ref` locally,
calls `message_push`, and settles with claim/fencing tokens. Gateway readiness
requires the Control Plane migration, notification baseline/reconciler,
selected channel module, and an empty legacy pending queue; it never starts a
claim while those gates are not ready. Worker steps, tool traces, runtime
names, heartbeats, and token usage are never pushed proactively.

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
available from the Web control-room entry point. Gateway delivery is
at-least-once: a provider success followed by a lost ack may produce one
recognizable duplicate using the same notification reference and idempotency
key. Provider calls occur outside Control Plane transactions, and raw provider
errors, route identities, and message bodies are never sent in nack payloads.
To roll back to `legacy`, stop and drain gateway claims first, then switch the
mode; do not run both modes concurrently.

## Development and release

The canonical source is `integrations/akashic/`. The Akashic snapshot under
`akashic-agent/` is read-only. Run the local checks with the project environment
and the host source on `PYTHONPATH`:

```bash
PYTHONPATH="$PWD/integrations/akashic:$PWD/akashic-agent" \
  uv run --project integrations/akashic pytest integrations/akashic/tests -q
PYTHONPATH="$PWD/integrations/akashic:$PWD/akashic-agent" \
  uv run --project integrations/akashic ruff check integrations/akashic
PYTHONPATH="$PWD/integrations/akashic:$PWD/akashic-agent" \
  uv run --project integrations/akashic pyright integrations/akashic/plugin.py integrations/akashic/sidera_task_bridge
```

### Existing Pinned Distribution Workflow

The steps below describe the existing/pinned Bridge distribution workflow;
they are not a verified installer path for the new client-dependent source.
This change does not update the currently pinned source, global plugin, or
runtime. A new distribution needs the matching client wheel or a fixed
published client version available to the installer, included in the
dependency closure. Verify that closure through the official installer before
using a new split. The subtree split shown below excludes
`packages/control-plane-client/`, and this dependency closure has not been
verified for an installed plugin.

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

When the Control Plane calls a local Knowledge Service, its internal HTTP
adapter bypasses inherited system proxy settings so macOS proxy configuration
cannot redirect loopback traffic. Keep both service origins on loopback unless
the separately secured remote opt-in is intentionally enabled. Candidate review and
Knowledge revision publication remain Web/owner operations; the bridge never copies
that state machine. Installing this bridge does not by itself start the real
Planner or Worker processes; configure them explicitly before submitting live work.
