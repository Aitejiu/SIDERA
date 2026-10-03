# sidera-agents

`sidera-agents` is a standalone Python package for agent profiles, runtime
capabilities, execution contracts, and run observations. Its public API uses
serializable contracts and generic authorization, context, secret, and tool
connection ports; it does not depend on SIDERA task or product internals.

## Install and Compose

Python 3.12 or newer is required. From the repository root, install into your
own virtual environment:

```sh
python -m pip install ./packages/agents
```

The core depends only on Pydantic, not CP, Product, Akashic, a native LLM SDK
or the PostgreSQL stack. For persistence, install the optional extra instead:

```sh
python -m pip install './packages/agents[postgres]'
```

Use the public factory and composition helpers; no private store/registry
imports are needed:

```python
from sidera_agents.bootstrap import (
    create_agent_service,
    create_harness_registry,
    open_memory_store,
)

service = create_agent_service(
    open_memory_store(),
    create_harness_registry((trusted_adapter,)),
    authorizer,
    context_reader,
    secret_reader,
    clock,
)
```

Supply your trusted adapter and implementations of the public `ports`
interfaces. `clock` returns an aware UTC datetime. Authorization is mandatory:
the authorizer receives the exact grant, action and target, and must reject
ungranted operations. Context, secrets and tool connections remain explicit
authorized ports; the `Deny*Reader` implementations reject unsupported access.

`open_memory_store()` is explicit ephemeral storage. Profiles, observations
and native-handle evidence disappear when it is discarded; it is never a
fallback for an unavailable persistent store. For PostgreSQL, use public
`bootstrap.open_postgres_store(database_url)` with a caller-supplied URL, pass
that store to the same factory, and run `service.migrate()` explicitly with
the necessary permissions for the owned `agents` schema. Readiness never
migrates or seeds the store.

`service.readiness()` checks storage before or after starting; PostgreSQL
requires the current migration head and all owned required tables, not just
a reachable connection. `service.readiness(require_execution_owner=True)`
additionally checks a locally claimed, nonclosing owner, any started background
worker's liveness, and current owner-scoped store evidence. These diagnostic
reads do not acquire/release ownership, poll or start native work. Strict
readiness is false before ownership is claimed and on uncertain evidence;
it is not a durable lease or execution authorization.

Start service driving explicitly with `service.start_background()` or manual
`service.poll_once()`; close with `service.close()`. Close may report
`shutdown_pending` while native work remains uncertain, retaining its existing
ownership/cleanup protections. Dispose a persistent store with `store.close()`
after service shutdown when its engine is no longer needed.

The public `api.AgentCatalog`, `api.AgentExecution` and `api.AgentObservations`
contracts expose configuration, execution and observations. Exact Profile and
Resource Refs pin immutable revisions; `get_profile_status(ref, grant_id=...)`
and `get_resource_status(ref, grant_id=...)` authorize those Refs, verify their
revision exists, then return the identity's **current** enabled/version state,
not historical state. Disabling an identity does not erase readable revisions.

## Trusted Adapter Extension

Implement `ports.HarnessAdapter` with `harness_id`, `executable_name`,
`describe`, `probe(candidate, checked_at)`, `validate`, `open`, `prompt`,
`read_events`, `cancel`, `inspect` and `close`. Use existing `contracts.Native*`
DTOs for the native boundary; consumer APIs do not branch on vendor handles.
`create_harness_registry((adapter,))` selects exactly those adapters and rejects
invalid/duplicate identities or missing callable methods. An explicit empty
iterable stays empty. With no argument, the helper lazily selects built-in
OpenCode without probing, spawning or reading authentication.

Adapters are trusted Python implementations, not sandboxed third-party plugins.
There is no dynamic plugin download/installer or automatic global registration.
Only OpenCode is built in; Codex, Hermes and DSH adapters are not implemented.

The provider-free [standalone example](examples/standalone.py) uses only public
imports. Its test-only `fixture-harness` creates/revises a Profile and Resource,
reads mutable status, starts a bounded Run, emits one safe message, confirms
close, and reads retained observations after close. It owns a temporary
executable/workspace and an explicit workspace/grant authorizer. Its simulated
native evidence does not establish actual model/authentication/tool execution.

After installing the package, run from the repository root:

```sh
python packages/agents/examples/standalone.py
```

## OpenCode Prerequisites and Limits

The built-in OpenCode adapter requires a local OpenCode executable. An explicit
`model_id` is a bounded `provider/model` route; it sets both the native model and
small model with that provider alone enabled. No model catalog or fallback is
invented. `None` applies no model override. Selected configuration is separate
from observed generation: model availability and authentication remain
unconfirmed until a native model response supplies evidence.

Provider credentials come from Profile `secret_refs` and the authorized
`SecretReader`. `native_settings.provider_env` selects the bound environment
name, never a key value. For explicit `opencode` or `opencode-go` models a bound
`OPENCODE_API_KEY` is the conventional default. Native JSON contains only an
environment placeholder; the resolved `SecretStr` is delivered to the child.
Run-local HOME/XDG directories isolate global settings and credentials. Native
login inheritance is unsupported: the adapter does not read or copy global auth,
use Keychain, change a CLI login or select a substitute account.

`native_settings.native_tools` accepts `none` (default), `read` (read/glob/grep)
or `write` (also edit/bash). Read/write require `workspace.execute` on the exact
prepared `ExecutionWorkspace`; write also requires `writable=True`. The pinned
workspace grant is rechecked before subsequent prompts. Bound MCP permissions
remain scoped separately. Task/subagent, skill discovery, network and external
directory permissions stay denied. Native permissions are not an OS sandbox;
in particular bash is native execution authority, not filesystem confinement.
Managed or ancestor project configuration causes explicit preflight rejection.
Thinking and undeclared native settings remain unsupported.

The default trusted registry uses independent host deadlines: setup RPC 30s,
prompt 900s, cancel confirmation 1s, close RPC 1s and process stop grace 1s.
Explicitly constructed adapters may override each deadline; optional prompt,
cancel or close values of `None` preserve the constructor's request timeout.
Timeouts must be positive finite numbers, excluding booleans. A prompt timeout
keeps the original response unknown and retains uncertain resources; a long
prompt budget does not extend Stop/cancel or prove confirmed cleanup.
