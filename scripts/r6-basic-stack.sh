#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CP_ROOT="$ROOT/services/control-plane"
KS_ROOT="$ROOT/services/knowledge-service"
MG_ROOT="$ROOT/services/main-agent-gateway"
WEB_ROOT="$ROOT/apps/web"
AKA_ROOT="$ROOT/akashic-agent"
UV_BIN=${SIDERA_UV_BIN:-uv}
PYTHON_BIN=${SIDERA_PYTHON_BIN:-python3}
CURL_BIN=${SIDERA_CURL_BIN:-curl}
COMPONENTS="knowledge-service control-plane basic-acceptance-heartbeat basic-worker-heartbeat akashic-acceptance planning-worker context-package-worker knowledge-candidate-worker activation-worker scheduler-worker reconciler-worker gateway-worker plan-transition-worker collaboration-worker notification-worker result-worker main-agent-gateway web"
ACCEPTANCE_COMPONENTS="knowledge-service control-plane basic-acceptance-heartbeat akashic-acceptance planning-worker context-package-worker knowledge-candidate-worker activation-worker scheduler-worker reconciler-worker gateway-worker plan-transition-worker collaboration-worker notification-worker result-worker main-agent-gateway web"
INTERACTIVE_COMPONENTS="knowledge-service control-plane basic-worker-heartbeat akashic-acceptance planning-worker context-package-worker knowledge-candidate-worker activation-worker scheduler-worker reconciler-worker gateway-worker plan-transition-worker collaboration-worker notification-worker result-worker main-agent-gateway web"

usage() {
  printf 'usage: %s status|start|stop|restart --manifest PATH [--component NAME] [--bootstrap-akashic]\n' "$0" >&2
}

ACTION=${1:-}
shift || true
MANIFEST=
COMPONENT=
BOOTSTRAP_AKASHIC=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --manifest) [ "$#" -gt 1 ] || { usage; exit 64; }; MANIFEST=$2; shift 2 ;;
    --component) [ "$#" -gt 1 ] || { usage; exit 64; }; COMPONENT=$2; shift 2 ;;
    --bootstrap-akashic) BOOTSTRAP_AKASHIC=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 64 ;;
  esac
done
case "$ACTION" in status|start|stop|restart) ;; *) usage; exit 64 ;; esac
[ -n "$MANIFEST" ] || { usage; exit 64; }
[ -f "$MANIFEST" ] || { printf 'manifest not found: %s\n' "$MANIFEST" >&2; exit 66; }

has_component() { case " $COMPONENTS " in *" $1 "*) return 0;; *) return 1;; esac; }
if [ -n "$COMPONENT" ] && ! has_component "$COMPONENT"; then
  printf 'unknown component: %s\n' "$COMPONENT" >&2
  exit 64
fi

manifest_value() {
  "$PYTHON_BIN" - "$MANIFEST" "$1" <<'PY'
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    for key in sys.argv[2].split("."):
        value = value[key]
except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"manifest field unavailable: {sys.argv[2]}") from exc
if not isinstance(value, (str, int, float)):
    raise SystemExit(f"manifest field is not scalar: {sys.argv[2]}")
print(value)
PY
}

RUN_ID=$(manifest_value run_id)
WORKSPACE_ID=$(manifest_value workspace_id)
RUN_ROOT=$(manifest_value run_root)
REPOSITORY=$(manifest_value repository)
BASE_COMMIT=$(manifest_value base_commit)
CONTROL_URL=$(manifest_value control_plane_url)
KNOWLEDGE_URL=$(manifest_value knowledge_url)
GATEWAY_URL=$(manifest_value main_agent_gateway_url)
WEB_URL=$(manifest_value web_url)
AKASHIC_URL=${SIDERA_R6_BASIC_AKASHIC_URL:-http://127.0.0.1:2236}
RUNTIME=${SIDERA_R6_BASIC_RUNTIME_DIR:-"$RUN_ROOT/.sidera-runtime/r6-basic"}
BASIC_MODE=${SIDERA_R6_BASIC_MODE:-acceptance}
case "$BASIC_MODE" in
  acceptance|interactive) ;;
  *)
    printf 'unsupported SIDERA_R6_BASIC_MODE: %s\n' "$BASIC_MODE" >&2
    exit 64
    ;;
esac
RESTORE_MODE=$("$PYTHON_BIN" - "$MANIFEST" <<'PY'
import json
import sys
from pathlib import Path

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get(
        "restore_mode", ""
    )
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit("restore mode unavailable") from exc
if value is None:
    value = ""
if value not in {"", "read_only"}:
    raise SystemExit("unsupported restore mode")
print(value)
PY
)
READ_ONLY_MODE=0
if [ "$RESTORE_MODE" = "read_only" ]; then
  READ_ONLY_MODE=1
fi
if [ "$BOOTSTRAP_AKASHIC" -eq 1 ]; then
  if [ "$ACTION" != start ]; then
    printf '%s\n' '--bootstrap-akashic is valid only with start' >&2
    exit 64
  fi
  if [ -n "$COMPONENT" ]; then
    printf '%s\n' '--bootstrap-akashic cannot be combined with --component' >&2
    exit 64
  fi
  if [ "$BASIC_MODE" != acceptance ] || [ "$READ_ONLY_MODE" -eq 1 ]; then
    printf '%s\n' '--bootstrap-akashic requires acceptance mode and a writable manifest' >&2
    exit 64
  fi
fi
if [ "$ACTION" = restart ] && [ "$READ_ONLY_MODE" -eq 1 ] \
  && [ "$COMPONENT" = akashic-acceptance ]; then
  printf '%s\n' 'Akashic restart is unavailable in read-only restore mode' >&2
  exit 64
fi
READ_ONLY_COMPONENTS="knowledge-service control-plane main-agent-gateway web"
PID_DIR="$RUNTIME/pids"
ID_DIR="$RUNTIME/identity"
LOG_DIR="$RUNTIME/logs"
ENV_DIR="$RUNTIME/env"
STALE_DIR="$RUNTIME/stale"

write_maintenance_inventory() {
  "$PYTHON_BIN" - "$MANIFEST" "$RUN_ROOT" <<'PY'
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

manifest_path = Path(sys.argv[1]).expanduser().resolve()
run_root = Path(sys.argv[2]).expanduser().resolve()
raw = json.loads(manifest_path.read_text(encoding="utf-8"))
schema_version = raw.get("schema_version", 1)
inventory_ref = raw.get("maintenance_inventory_ref")
if schema_version == 2:
    if not isinstance(inventory_ref, str) or not inventory_ref:
        raise SystemExit("schema 2 acceptance manifest requires maintenance inventory")
    inventory_path = Path(inventory_ref.removeprefix("file:")).expanduser()
    if not inventory_path.is_absolute() or inventory_path.resolve(strict=False).parent != run_root:
        raise SystemExit("maintenance inventory must be an absolute direct child of run_root")
    if not inventory_path.is_file() or inventory_path.is_symlink():
        raise SystemExit("schema 2 maintenance inventory is missing")
else:
    inventory_path = run_root / "maintenance-inventory.json"
    raw["maintenance_inventory_ref"] = str(inventory_path)
    if inventory_path.is_file() and not inventory_path.is_symlink():
        raise SystemExit(0)
    temporary_manifest = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
    try:
        temporary_manifest.write_text(
            json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary_manifest, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary_manifest, manifest_path)
    finally:
        try:
            temporary_manifest.unlink()
        except OSError:
            pass

components = [
    {
        "component_id": "control-plane-postgres",
        "kind": "postgres",
        "source_ref": f"postgres:ref:{raw.get('database_ref', 'unknown')}",
        "restore_relative_path": "databases/control-plane",
        "schema_identity": "migration:control-plane",
        "required": True,
        "secret_policy": "private_data",
    },
    {
        "component_id": "knowledge-postgres",
        "kind": "postgres",
        "source_ref": f"postgres:ref:{raw.get('knowledge_database_ref', 'unknown')}",
        "restore_relative_path": "databases/knowledge",
        "schema_identity": "migration:knowledge-service",
        "required": True,
        "secret_policy": "private_data",
    },
]
payload = {
    "schema_version": 1,
    "run_id": str(raw["run_id"]),
    "workspace_id": str(raw["workspace_id"]),
    "code_commit": str(raw.get("code_commit", raw.get("base_commit", ""))),
    "implementation_fingerprint": str(raw.get("implementation_fingerprint", "0" * 64)),
    "runtime_fingerprint": str(raw.get("runtime_fingerprint", "0" * 64)),
    "writers": {
        "status": "unknown",
        "active_component_ids": ["stack"],
        "observed_at": "generated-at-stack-start",
    },
    "persistence": {
        "status": "incomplete",
        "component_ids": [item["component_id"] for item in components],
        "unsupported_component_ids": [item["component_id"] for item in components],
    },
    "components": components,
    "source_identities": {
        "database_ref": raw.get("database_ref"),
        "knowledge_database_ref": raw.get("knowledge_database_ref"),
    },
}
if schema_version == 2:
    existing = json.loads(inventory_path.read_text(encoding="utf-8"))
    if existing.get("run_id") != payload["run_id"] or existing.get("workspace_id") != payload["workspace_id"]:
        raise SystemExit("maintenance inventory identity mismatch")
else:
    run_root.mkdir(parents=True, exist_ok=True, mode=stat.S_IRWXU)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{inventory_path.name}.", dir=run_root, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary_name, inventory_path)
    finally:
        try:
            Path(temporary_name).unlink()
        except OSError:
            pass
PY
}

update_maintenance_writer_state() {
  writer_status=$1
  shift
  "$PYTHON_BIN" - "$MANIFEST" "$writer_status" "$@" <<'PY'
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

manifest_path = Path(sys.argv[1]).expanduser().resolve()
status = sys.argv[2]
active = list(sys.argv[3:])
raw = json.loads(manifest_path.read_text(encoding="utf-8"))
reference = raw.get("maintenance_inventory_ref")
if not isinstance(reference, str) or not reference:
    raise SystemExit(0)
inventory_path = Path(reference.removeprefix("file:")).expanduser().resolve(strict=False)
if not inventory_path.is_file() or inventory_path.is_symlink():
    raise SystemExit("maintenance inventory is missing")
payload = json.loads(inventory_path.read_text(encoding="utf-8"))
payload["writers"] = {
    "status": status,
    "active_component_ids": active,
    "observed_at": datetime.now(timezone.utc).isoformat(),
}
descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{inventory_path.name}.", dir=inventory_path.parent, text=True
)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary_name, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary_name, inventory_path)
finally:
    try:
        Path(temporary_name).unlink()
    except OSError:
        pass
PY
}

LOCK_DIR="$RUN_ROOT/.sidera-maintenance.lock"
LOCK_OWNER="$LOCK_DIR/owner.json"
LOCK_HELD=0

release_run_lock() {
  [ "$LOCK_HELD" -eq 1 ] || return 0
  owner_pid=$($PYTHON_BIN - "$LOCK_OWNER" <<'PY'
import json
import sys
from pathlib import Path
try:
    print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("pid", ""))
except (OSError, json.JSONDecodeError):
    print("")
PY
  )
  if [ "$owner_pid" = "$$" ]; then
    rm -f "$LOCK_OWNER"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  LOCK_HELD=0
}

acquire_run_lock() {
  mkdir -p "$RUN_ROOT"
  chmod 700 "$RUN_ROOT"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    owner_pid=$($PYTHON_BIN - "$LOCK_OWNER" <<'PY'
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("pid", "")
    print(value)
except (OSError, json.JSONDecodeError):
    print("")
PY
    )
    case "$owner_pid" in
      ''|*[!0-9]*)
        printf 'maintenance busy: run lock owner is unavailable\n' >&2
        return 75
        ;;
      *)
        if kill -0 "$owner_pid" >/dev/null 2>&1; then
          printf 'maintenance busy: run lock owned by pid %s\n' "$owner_pid" >&2
          return 75
        fi
        rm -f "$LOCK_OWNER"
        rmdir "$LOCK_DIR" 2>/dev/null || true
        mkdir "$LOCK_DIR" 2>/dev/null || {
          printf 'maintenance busy: run lock changed during stale cleanup\n' >&2
          return 75
        }
        ;;
    esac
  fi
  printf '{"pid":%s,"owner":"r6-basic-stack"}\n' "$$" > "$LOCK_OWNER"
  chmod 600 "$LOCK_OWNER"
  LOCK_HELD=1
  trap release_run_lock EXIT
}

acquire_run_lock || exit $?
write_maintenance_inventory
mkdir -p "$PID_DIR" "$ID_DIR" "$LOG_DIR" "$ENV_DIR" "$STALE_DIR"
chmod 700 "$RUNTIME" "$PID_DIR" "$ID_DIR" "$LOG_DIR" "$ENV_DIR" "$STALE_DIR"

pid_path() { printf '%s/%s.pid\n' "$PID_DIR" "$1"; }
id_path() { printf '%s/%s.identity\n' "$ID_DIR" "$1"; }
log_path() { printf '%s/%s.log\n' "$LOG_DIR" "$1"; }
env_path() { printf '%s/%s.json\n' "$ENV_DIR" "$1"; }

id_value() {
  "$PYTHON_BIN" - "$1" "$2" <<'PY'
import sys
from pathlib import Path
try:
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
        if line.startswith(sys.argv[2] + "="):
            print(line[len(sys.argv[2]) + 1:])
            break
except OSError:
    pass
PY
}

ps_field() {
  "$PYTHON_BIN" - "$1" "$2" <<'PY'
import subprocess
import sys
try:
    value = subprocess.check_output(["ps", "-p", sys.argv[1], "-o", sys.argv[2]], text=True, stderr=subprocess.DEVNULL).strip()
except (OSError, subprocess.CalledProcessError):
    raise SystemExit(1)
if not value:
    raise SystemExit(1)
print(value)
PY
}

hash_argv() { printf '%s' "$1" | shasum -a 256 | awk '{print $1}'; }
alive() { kill -0 "$1" >/dev/null 2>&1; }
state_present() { [ -f "$(pid_path "$1")" ] || [ -f "$(id_path "$1")" ]; }

signal_owned() {
  signal=$1
  pid=$2
  pgid=$(ps_field "$pid" pgid= 2>/dev/null | tr -d ' ' || true)
  if [ "$pgid" = "$pid" ]; then
    kill "-$signal" -- "-$pgid" >/dev/null 2>&1
  else
    kill "-$signal" "$pid" >/dev/null 2>&1
  fi
}

signature() {
  if [ "${SIDERA_R6_BASIC_TEST_MODE:-0}" = 1 ]; then
    printf '%s %s\n' "${SIDERA_R6_BASIC_STUB_COMMAND:?SIDERA_R6_BASIC_STUB_COMMAND}" "$1"
    return
  fi
  case "$1" in
    knowledge-service) printf 'sidera_knowledge.api.app:app\n' ;;
    control-plane) printf 'sidera_control_plane.api.app:create_app\n' ;;
    main-agent-gateway) printf 'sidera_main_agent_gateway.api.app:create_app\n' ;;
    web) printf 'vite/bin/vite.js\n' ;;
    akashic-acceptance) printf 'main.py --config\n' ;;
    *) printf 'sidera_control_plane.%s\n' "$(printf '%s' "$1" | tr '-' '_')" ;;
  esac
}

quarantine() {
  stamp=$(date +%s)
  for path in "$(pid_path "$1")" "$(id_path "$1")"; do
    [ -f "$path" ] || continue
    mv "$path" "$STALE_DIR/${1}.$stamp.$(basename "$path")"
  done
}

trusted() {
  component=$1
  pidfile=$(pid_path "$component")
  idfile=$(id_path "$component")
  [ -f "$pidfile" ] && [ -f "$idfile" ] || return 1
  pid=$(sed -n '1p' "$pidfile" 2>/dev/null || true)
  case "$pid" in ''|*[!0-9]*) return 1;; esac
  alive "$pid" || return 1
  [ "$(id_value "$idfile" pid)" = "$pid" ] || return 1
  [ "$(id_value "$idfile" run_id)" = "$RUN_ID" ] || return 1
  [ "$(id_value "$idfile" workspace_id)" = "$WORKSPACE_ID" ] || return 1
  [ "$(id_value "$idfile" component)" = "$component" ] || return 1
  [ "$(id_value "$idfile" marker)" = "sidera-r6-basic:$RUN_ID:$component" ] || return 1
  [ "$(id_value "$idfile" process_start)" = "$(ps_field "$pid" lstart= 2>/dev/null || true)" ] || return 1
  command=$(ps_field "$pid" command= 2>/dev/null || true)
  [ -n "$command" ] || return 1
  [ "$(id_value "$idfile" argv_hash)" = "$(hash_argv "$command")" ] || return 1
  case "$command" in *"$(signature "$component")"*) return 0;; *) return 1;; esac
}

url_port() {
  "$PYTHON_BIN" - "$1" <<'PY'
import sys
from urllib.parse import urlsplit
parsed = urlsplit(sys.argv[1])
print(parsed.port or (443 if parsed.scheme == "https" else 80))
PY
}

url_host() {
  "$PYTHON_BIN" - "$1" <<'PY'
import sys
from urllib.parse import urlsplit
host = urlsplit(sys.argv[1]).hostname
if not host:
    raise SystemExit("missing URL host")
print(host)
PY
}

component_url() {
  case "$1" in
    knowledge-service) printf '%s\n' "$KNOWLEDGE_URL" ;;
    control-plane) printf '%s\n' "$CONTROL_URL" ;;
    main-agent-gateway) printf '%s\n' "$GATEWAY_URL" ;;
    web) printf '%s\n' "$WEB_URL" ;;
    akashic-acceptance) printf '%s\n' "$AKASHIC_URL" ;;
    *) return 1 ;;
  esac
}

occupied() {
  url=$(component_url "$1" 2>/dev/null || true)
  [ -n "$url" ] || return 1
  host=$(url_host "$url")
  port=$(url_port "$url")
  "$PYTHON_BIN" - "$host" "$port" <<'PY'
import socket
import sys
sock = socket.socket(socket.AF_INET6 if ":" in sys.argv[1] else socket.AF_INET)
sock.settimeout(.15)
try:
    code = sock.connect_ex((sys.argv[1], int(sys.argv[2])))
finally:
    sock.close()
raise SystemExit(0 if code == 0 else 1)
PY
}

resolve_planning_profile() {
  override_id=${SIDERA_R6_BASIC_PLANNING_PROFILE_ID:-}
  override_version=${SIDERA_R6_BASIC_PLANNING_PROFILE_VERSION:-}
  if [ -n "$override_id" ] || [ -n "$override_version" ]; then
    if [ -z "$override_id" ] || [ -z "$override_version" ]; then
      printf 'planning profile id and version overrides must be provided together\n' >&2
      return 64
    fi
    "$PYTHON_BIN" - "$override_id" "$override_version" <<'PY'
import sys
from uuid import UUID

profile_id = UUID(sys.argv[1])
profile_version = int(sys.argv[2])
if profile_version < 1:
    raise SystemExit("planning profile version must be positive")
print(f"{profile_id}|{profile_version}")
PY
    return
  fi

  run_hex=$(printf '%s' "$RUN_ID" | tr -d '-')
  profile_key=$(printf 'r6-basic-%.12s' "$run_hex")
  "$PYTHON_BIN" - "$CONTROL_URL" "$WORKSPACE_ID" "$profile_key" <<'PY'
import json
import sys
from urllib.parse import quote
from urllib.request import Request, urlopen

control_url, workspace_id, profile_key = sys.argv[1:]
url = (
    f"{control_url.rstrip('/')}/api/v1/workspaces/"
    f"{quote(workspace_id, safe='')}/planning-profiles?limit=100"
)
request = Request(url, headers={"Accept": "application/json"})
try:
    with urlopen(request, timeout=5.0) as response:
        payload = json.load(response)
except Exception as exc:
    raise SystemExit(f"planning profile lookup failed: {exc}") from exc
items = payload.get("items") if isinstance(payload, dict) else None
if not isinstance(items, list):
    raise SystemExit("planning profile lookup returned an invalid page")
matches = [
    item
    for item in items
    if isinstance(item, dict)
    and item.get("profile_key") == profile_key
    and item.get("status") == "active"
    and item.get("user_selectable") is True
]
if len(matches) != 1:
    raise SystemExit(
        f"expected one active planning profile {profile_key}, found {len(matches)}"
    )
profile = matches[0]
profile_id = profile.get("id")
profile_version = profile.get("version")
if not isinstance(profile_id, str) or not isinstance(profile_version, int):
    raise SystemExit("planning profile identity is invalid")
print(f"{profile_id}|{profile_version}")
PY
}

write_akashic_bridge_config() {
  profile_ref=$(resolve_planning_profile) || return
  profile_id=${profile_ref%%|*}
  profile_version=${profile_ref#*|}
  bridge_config="$RUNTIME/akashic-workspace/plugin-data/sidera-task-bridge-sidera/config.local.toml"
  "$PYTHON_BIN" - "$bridge_config" "$WORKSPACE_ID" "$profile_id" "$profile_version" "$CONTROL_URL" "$WEB_URL" "$KNOWLEDGE_URL" <<'PY'
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from uuid import UUID

(
    target,
    workspace_id,
    profile_id,
    raw_profile_version,
    control_url,
    web_url,
    knowledge_url,
) = sys.argv[1:]
UUID(workspace_id)
UUID(profile_id)
profile_version = int(raw_profile_version)
if profile_version < 1:
    raise SystemExit("planning profile version must be positive")

values = (
    ("workspace_id", workspace_id),
    ("planning_profile_id", profile_id),
    ("planning_profile_version", profile_version),
    ("control_plane_url", control_url),
    ("web_base_url", web_url),
    ("knowledge_base_url", knowledge_url),
    ("actor_type", "integration"),
    ("actor_id", "akashic-task-bridge"),
)
lines = [
    f"{key} = {value if isinstance(value, int) else json.dumps(value)}"
    for key, value in values
]
config_path = Path(target)
config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{config_path.name}.", dir=config_path.parent, text=True
)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary_name, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary_name, config_path)
finally:
    try:
        os.unlink(temporary_name)
    except OSError:
        pass
PY
}

akashic_server_ready() {
  socket_path="$RUNTIME/akashic-workspace/akashic.sock"
  [ -S "$socket_path" ] || return 1
  "$PYTHON_BIN" - "$socket_path" <<'PY'
import json
import socket
import sys

client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(0.2)
try:
    client.connect(sys.argv[1])
    stream = client.makefile("rwb", buffering=0)

    def send(payload):
        stream.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")

    def response(request_id):
        while True:
            raw = stream.readline()
            if not raw:
                raise RuntimeError("Akashic control socket closed during readiness")
            message = json.loads(raw)
            if message.get("id") == request_id:
                return message

    send({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "1.0",
            "clientInfo": {"name": "sidera-basic-stack", "version": "1.0"},
            "capabilities": {"reasoningEvents": False},
        },
    })
    initialized = response(1)
    if "error" in initialized:
        raise RuntimeError("Akashic initialize failed")
    send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    send({"jsonrpc": "2.0", "id": 2, "method": "server/status", "params": {}})
    status = response(2)
    if status.get("result", {}).get("ready") is not True:
        raise RuntimeError("Akashic server is not ready")
except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
    raise SystemExit(1)
finally:
    client.close()
PY
}

akashic_plugin_gate() {
  if [ "${SIDERA_R6_BASIC_TEST_MODE:-0}" = 1 ]; then
    [ "${SIDERA_R6_BASIC_TEST_AKASHIC_GATE:-0}" = 1 ]
    return
  fi
  "$UV_BIN" run --project "$AKA_ROOT" python \
    "$ROOT/scripts/r6_akashic_plugin_gate.py" \
    --manifest "$MANIFEST" --runtime "$RUNTIME" --json
}

akashic_bridge_ready() {
  akashic_plugin_gate >/dev/null 2>&1 || return 1
  akashic_server_ready
}

write_env() {
  component=$1
  target=$(env_path "$component")
  "$PYTHON_BIN" - "$MANIFEST" "$target" "$component" "$RUNTIME" "$CONTROL_URL" "$KNOWLEDGE_URL" "$GATEWAY_URL" "$WEB_URL" "$AKASHIC_URL" "$BASIC_MODE" <<'PY'
import json
import hashlib
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

(
    manifest_path,
    target,
    component,
    runtime,
    control_url,
    knowledge_url,
    gateway_url,
    web_url,
    akashic_url,
    basic_mode,
) = sys.argv[1:]
if basic_mode not in {"acceptance", "interactive"}:
    raise SystemExit("unsupported basic stack mode")
manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
env_ref = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
allowed = {
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "TMPDIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "UV_CACHE_DIR",
    "SIDERA_CREDENTIAL_STORE_READY",
    "SIDERA_ACCEPTANCE_SCENARIO",
    "SIDERA_ACCEPTANCE_GROUP",
    "SIDERA_ASSIGNMENT_LEASE_SECONDS",
    "SIDERA_ASSIGNMENT_LEASE_RENEW_INTERVAL_SECONDS",
    "SIDERA_GATEWAY_OWNER_START_CONCURRENCY",
    "SIDERA_RESULT_INPUTS_ENABLED",
    "SIDERA_RESULT_INTEGRATION_ENABLED",
    "SIDERA_RESULT_REPOSITORY_REF",
    "SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS",
    "SIDERA_OPENCODE_REQUEST_TIMEOUT_SECONDS",
}
selected = set()
for key in ("database_ref", "knowledge_database_ref"):
    value = manifest.get(key)
    if isinstance(value, str) and env_ref.fullmatch(value):
        selected.add(value[4:])
profile_components = {
    "main_agent": "akashic-acceptance",
    "planner": "planning-worker",
    "worker": "gateway-worker",
    "second_worker": "gateway-worker",
}
for profile in manifest.get("profiles", []):
    if (
        not isinstance(profile, dict)
        or profile_components.get(profile.get("role")) != component
    ):
        continue
    for key in ("credential_ref", "config_ref"):
        value = profile.get(key)
        if isinstance(value, str) and env_ref.fullmatch(value):
            selected.add(value[4:])

def resolve(value):
    if env_ref.fullmatch(value):
        name = value[4:]
        if name not in os.environ:
            raise SystemExit(f"selected environment reference is missing: {name}")
        return os.environ[name]
    if value.startswith("file:"):
        return Path(value[5:]).read_text(encoding="utf-8").strip()
    if value == "none":
        return ""
    return value

def url_port(value):
    port = urlsplit(value).port
    if port is None:
        raise SystemExit(f"service URL has no port: {value}")
    return str(port)

token_path = Path(runtime) / "tokens.json"
token_keys = (
    "knowledge_local_owner",
    "knowledge_main_agent",
    "knowledge_control_plane",
    "web_knowledge",
)

def load_or_create_tokens():
    if token_path.exists() or token_path.is_symlink():
        if token_path.is_symlink() or not token_path.is_file():
            raise SystemExit("capability-token file must be a regular file")
        if token_path.stat().st_mode & 0o077:
            raise SystemExit("capability-token file permissions are too broad")
        try:
            raw = json.loads(token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit("capability-token file is invalid") from exc
        if (
            not isinstance(raw, dict)
            or set(raw) != set(token_keys)
            or any(
                not isinstance(raw[key], str) or len(raw[key]) < 32
                for key in token_keys
            )
        ):
            raise SystemExit("capability-token file is invalid")
        return raw

    token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tokens = {key: secrets.token_urlsafe(32) for key in token_keys}
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{token_path.name}.", dir=token_path.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(tokens, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, token_path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return tokens

tokens = load_or_create_tokens()

manifest_path = Path(manifest_path).expanduser().resolve()
repository_path = Path(manifest["repository"]).expanduser().resolve(strict=False)
repository_ref = "git-repository://" + hashlib.sha256(
    str(repository_path).encode("utf-8")
).hexdigest()
guard_enabled = (
    manifest.get("schema_version") == 2
    and basic_mode == "acceptance"
)
route_ids = {}
route_facts = {}
if guard_enabled:
    routes = manifest.get("allowed_routes")
    if not isinstance(routes, list):
        raise SystemExit("schema 2 acceptance manifest requires allowed routes")
    profiles = {
        item.get("role"): item
        for item in manifest.get("profiles", [])
        if isinstance(item, dict) and isinstance(item.get("role"), str)
    }
    for route in routes:
        if not isinstance(route, dict):
            raise SystemExit("acceptance route must be an object")
        role = route.get("role")
        route_id = route.get("route_id")
        if not isinstance(role, str) or not isinstance(route_id, str) or not route_id:
            raise SystemExit("acceptance route identity is invalid")
        if role in route_ids:
            raise SystemExit(f"acceptance manifest has multiple routes for role: {role}")
        profile = profiles.get(role)
        if not isinstance(profile, dict):
            raise SystemExit(f"acceptance manifest is missing profile for role: {role}")
        for field in (
            "runtime",
            "provider",
            "model_id",
            "endpoint_host",
            "credential_ref",
            "config_version",
            "fallbacks",
        ):
            profile_value = profile.get(field, [] if field == "fallbacks" else None)
            route_value = route.get(field, [] if field == "fallbacks" else None)
            if profile_value != route_value:
                raise SystemExit(
                    f"acceptance profile and route differ for {role}:{field}"
                )
        route_ids[role] = route_id
        route_facts[role] = {
            field: route.get(field, [] if field == "fallbacks" else None)
            for field in (
                "runtime",
                "provider",
                "model_id",
                "endpoint_host",
                "credential_ref",
                "config_version",
                "fallbacks",
            )
        }
        route_facts[role]["route_id"] = route_id
    missing_roles = {"main_agent", "planner", "worker", "second_worker"} - set(route_ids)
    if missing_roles:
        raise SystemExit(
            "acceptance manifest is missing routes: " + ",".join(sorted(missing_roles))
        )

child = {key: value for key, value in os.environ.items() if key in allowed or key in selected}
if component == "akashic-acceptance":
    plugin_home = Path(runtime).expanduser().resolve() / "akashic-plugin-home"
    if plugin_home.is_symlink():
        raise SystemExit("Akashic plugin home must not be a symlink")
    plugin_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(plugin_home, 0o700)
    child["AKASHIC_PLUGIN_HOME"] = str(plugin_home)
if basic_mode == "interactive":
    child.setdefault("SIDERA_RESULT_INPUTS_ENABLED", "1")
    child.setdefault("SIDERA_RESULT_INTEGRATION_ENABLED", "1")
child["SIDERA_DATABASE_URL"] = resolve(str(manifest["database_ref"]))
child["SIDERA_KNOWLEDGE_DATABASE_URL"] = resolve(str(manifest["knowledge_database_ref"]))
child.update({
    "SIDERA_WORKSPACE_ID": str(manifest["workspace_id"]),
    "SIDERA_CONTROL_PLANE_URL": control_url,
    "SIDERA_KNOWLEDGE_BASE_URL": knowledge_url,
    "SIDERA_MAIN_AGENT_GATEWAY_URL": gateway_url,
    "SIDERA_WEB_URL": web_url,
    "SIDERA_WEB_CONTROL_PLANE_PORT": url_port(control_url),
    "SIDERA_WEB_KNOWLEDGE_SERVICE_PORT": url_port(knowledge_url),
    "SIDERA_WEB_MAIN_AGENT_PORT": url_port(gateway_url),
    "SIDERA_WEB_PORT": url_port(web_url),
    "VITE_SIDERA_WORKSPACE_ID": str(manifest["workspace_id"]),
    "SIDERA_R6_BASIC_WORKSPACE_ID": str(manifest["workspace_id"]),
    "SIDERA_COLLABORATION_WORKSPACE_ID": str(manifest["workspace_id"]),
    "SIDERA_R6_BASIC_REPOSITORY": str(manifest["repository"]),
    "SIDERA_R6_BASIC_BASE_COMMIT": str(manifest["base_commit"]),
    "SIDERA_R6_BASIC_RUNTIME_FINGERPRINT": str(manifest["runtime_fingerprint"]),
    "SIDERA_R6_BASIC_COMPONENT": component,
    "SIDERA_R6_BASIC_RUNTIME_DIR": runtime,
    "SIDERA_R6_BASIC_HEARTBEAT_FILE": str(Path(runtime) / "heartbeats" / f"{component}.json"),
    "SIDERA_R6_BASIC_LOG_FILE": str(Path(runtime) / "logs" / f"{component}.log"),
    "SIDERA_R6_BASIC_NOTIFICATION_BASELINE_REQUIRED": "true",
    "SIDERA_NOTIFICATION_BASELINE_REQUIRED": "true",
    "SIDERA_RESULT_APPLY_ON_ACCEPT": "false",
    "SIDERA_RESULT_REPOSITORY_PATH": str(manifest["repository"]),
    "SIDERA_RESULT_REPOSITORY_REF": repository_ref,
    "SIDERA_LOCAL_GATE_REPOSITORY": str(manifest["repository"]),
    "SIDERA_RESULT_BASE_COMMIT": str(manifest["base_commit"]),
    "SIDERA_MAIN_AGENT_SIDERA_WORKSPACE_ID": str(manifest["workspace_id"]),
    "SIDERA_GATEWAY_SYNC_WORKSPACE_ID": str(manifest["workspace_id"]),
    "SIDERA_MAIN_AGENT_AKASHIC_ENDPOINT": str(Path(runtime) / "akashic-workspace" / "akashic.sock"),
    "SIDERA_AKASHIC_URL": akashic_url,
    "SIDERA_RESTORE_READ_ONLY": "true" if manifest.get("restore_mode") == "read_only" else "false",
    "SIDERA_RESTORE_SCHEMA_VALIDATION": "true" if manifest.get("restore_mode") == "read_only" else "false",
    "SIDERA_MAIN_AGENT_RESTORE_READ_ONLY": "true" if manifest.get("restore_mode") == "read_only" else "false",
    "SIDERA_KNOWLEDGE_RESTORE_READ_ONLY": "true" if manifest.get("restore_mode") == "read_only" else "false",
})
if component in {"basic-acceptance-heartbeat", "basic-worker-heartbeat"}:
    child["SIDERA_R6_BASIC_RUN_ID"] = str(manifest["run_id"])
if component == "basic-worker-heartbeat":
    child["SIDERA_R6_BASIC_HEARTBEAT_COMPONENT"] = component
if component == "knowledge-service":
    child.update({
        "SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN": tokens["knowledge_local_owner"],
        "SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN": tokens["knowledge_main_agent"],
        "SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN": tokens["knowledge_control_plane"],
    })
elif component == "akashic-acceptance":
    child["SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN"] = tokens["knowledge_main_agent"]
elif component in {
    "prepare",
    "baseline",
    "control-plane",
    "basic-acceptance-heartbeat",
    "planning-worker",
    "context-package-worker",
    "knowledge-candidate-worker",
    "activation-worker",
    "scheduler-worker",
    "reconciler-worker",
    "gateway-worker",
    "plan-transition-worker",
    "collaboration-worker",
    "notification-worker",
    "result-worker",
}:
    child["SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN"] = tokens[
        "knowledge_control_plane"
    ]
elif component == "web":
    child["SIDERA_WEB_KNOWLEDGE_TOKEN"] = tokens["knowledge_local_owner"]
if guard_enabled:
    child.update({
        "SIDERA_R6_BASIC_RUN_ID": str(manifest["run_id"]),
        "SIDERA_ACCEPTANCE_MANIFEST": str(manifest_path),
        "SIDERA_ACCEPTANCE_GUARD_REQUIRED": "1",
        "SIDERA_ACCEPTANCE_MAIN_AGENT_ROUTE_ID": route_ids["main_agent"],
        "SIDERA_ACCEPTANCE_PLANNER_ROUTE_ID": route_ids["planner"],
        "SIDERA_ACCEPTANCE_WORKER_SESSION_ROUTE_ID": route_ids["worker"],
        "SIDERA_ACCEPTANCE_WORKER_PROMPT_ROUTE_ID": route_ids["second_worker"],
        "SIDERA_ACCEPTANCE_SECOND_WORKER_ROUTE_ID": route_ids["second_worker"],
        "SIDERA_ACCEPTANCE_ROUTE_FACTS": json.dumps(
            route_facts, sort_keys=True, separators=(",", ":")
        ),
    })
if os.environ.get("SIDERA_R6_BASIC_TEST_MODE") == "1":
    for key in (
        "SIDERA_R6_BASIC_TEST_CAPTURE",
        "SIDERA_R6_BASIC_STUB_FAIL_COMPONENT",
        "SIDERA_R6_BASIC_STUB_IGNORE_TERM_COMPONENT",
        "SIDERA_R6_BASIC_TEST_AKASHIC_BRIDGE_READY",
    ):
        if key in os.environ:
            child[key] = os.environ[key]

path = Path(target)
path.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(child, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary, path)
finally:
    try:
        os.unlink(temporary)
    except OSError:
        pass
PY
  chmod 600 "$target"
}

sync_akashic_acceptance_credential() {
  [ "$BASIC_MODE" = acceptance ] || return 0
  [ "${SIDERA_R6_BASIC_TEST_MODE:-0}" = 1 ] && return 0
  write_env akashic-acceptance
  target=$(env_path akashic-acceptance)
  if ! "$UV_BIN" run --project "$AKA_ROOT" python \
    "$ROOT/scripts/r6_akashic_credentials.py" \
    --manifest "$MANIFEST" \
    --environment-file "$target" \
    --workspace "$RUNTIME/akashic-workspace"; then
    rm -f "$target"
    return 1
  fi
  rm -f "$target"
}

run_env() {
  component=$1
  shift
  write_env "$component"
  target=$(env_path "$component")
  "$PYTHON_BIN" - "$target" "$@" <<'PY'
import json
import os
import sys
from pathlib import Path
path = Path(sys.argv[1])
try:
    env = json.loads(path.read_text(encoding="utf-8"))
finally:
    try:
        path.unlink()
    except OSError:
        pass
os.environ.clear()
os.environ.update({str(k): str(v) for k, v in env.items()})
os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
PY
}

run_component() {
  component=$1
  shift
  write_env "$component"
  target=$(env_path "$component")
  log=$(log_path "$component")
  : > "$log"
  chmod 600 "$log"
  "$PYTHON_BIN" - "$target" "$@" <<'PY' >>"$log" 2>&1 &
import json
import os
import sys
from pathlib import Path
path = Path(sys.argv[1])
try:
    env = json.loads(path.read_text(encoding="utf-8"))
finally:
    try:
        path.unlink()
    except OSError:
        pass
os.environ.clear()
os.environ.update({str(k): str(v) for k, v in env.items()})
os.setsid()
os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
PY
  printf '%s\n' "$!"
}

start_command() {
  component=$1
  if [ "${SIDERA_R6_BASIC_TEST_MODE:-0}" = 1 ]; then
    run_component "$component" "${SIDERA_R6_BASIC_STUB_COMMAND:?SIDERA_R6_BASIC_STUB_COMMAND}" "$component"
    return
  fi
  case "$component" in
    knowledge-service) run_component "$component" "$UV_BIN" run --project "$KS_ROOT" uvicorn sidera_knowledge.api.app:app --host 127.0.0.1 --port "$(url_port "$KNOWLEDGE_URL")" ;;
    control-plane) run_component "$component" "$UV_BIN" run --project "$CP_ROOT" uvicorn sidera_control_plane.api.app:create_app --factory --host 127.0.0.1 --port "$(url_port "$CONTROL_URL")" ;;
    basic-acceptance-heartbeat) run_component "$component" "$UV_BIN" run --project "$CP_ROOT" python -u -m sidera_control_plane.basic_acceptance_heartbeat ;;
    basic-worker-heartbeat) run_component "$component" "$UV_BIN" run --project "$CP_ROOT" python -u -m sidera_control_plane.basic_worker_heartbeat ;;
    akashic-acceptance)
      config=${SIDERA_R6_BASIC_AKASHIC_CONFIG:-"$AKA_ROOT/config.toml"}
      workspace="$RUNTIME/akashic-workspace"
      mkdir -p "$workspace"
      run_component "$component" "$UV_BIN" run --project "$AKA_ROOT" python "$AKA_ROOT/main.py" --config "$config" --workspace "$workspace"
      ;;
    planning-worker|context-package-worker|knowledge-candidate-worker|activation-worker|scheduler-worker|reconciler-worker|gateway-worker|plan-transition-worker|collaboration-worker|notification-worker|result-worker)
      module=$(printf '%s' "$component" | tr '-' '_')
      run_component "$component" "$UV_BIN" run --project "$CP_ROOT" python -u -m "sidera_control_plane.$module"
      ;;
    main-agent-gateway) run_component "$component" "$UV_BIN" run --project "$MG_ROOT" uvicorn sidera_main_agent_gateway.api.app:create_app --factory --host 127.0.0.1 --port "$(url_port "$GATEWAY_URL")" ;;
    web)
      web_port=$(url_port "$WEB_URL")
      run_component "$component" /bin/sh -c \
        "cd \"$WEB_ROOT\" && exec \"$WEB_ROOT/node_modules/vite/bin/vite.js\" --host 127.0.0.1 --port $web_port"
      ;;
    *) return 1 ;;
  esac
}

ready() {
  component=$1
  if [ "$component" = akashic-acceptance ] \
    && [ "$BASIC_MODE" = acceptance ] \
    && [ "$BOOTSTRAP_AKASHIC" -ne 1 ]; then
    akashic_plugin_gate >/dev/null 2>&1 || return 1
  fi
  i=0
  while [ "$i" -lt 100 ]; do
    pid=$(cat "$(pid_path "$component")" 2>/dev/null || printf 0)
    alive "$pid" || return 1
    if [ "${SIDERA_R6_BASIC_TEST_MODE:-0}" = 1 ]; then
      if [ "$component" = akashic-acceptance ]; then
        akashic_server_ready && return 0
      elif grep -E '"status":"(ready|ok)"' "$(log_path "$component")" >/dev/null 2>&1 \
        && grep -E '"heartbeat":true|"diagnostic"' "$(log_path "$component")" >/dev/null 2>&1; then
        return 0
      fi
    else
      strict_akashic_readiness=0
      case "$component" in
        knowledge-service) health="$(component_url "$component")/health/ready" ;;
        control-plane) health="$(component_url "$component")/healthz" ;;
        main-agent-gateway) health="$(component_url "$component")/main-agent/v1/health" ;;
        web) health="$(component_url "$component")/" ;;
        akashic-acceptance)
          strict_akashic_readiness=1
          akashic_server_ready && return 0
          ;;
        basic-acceptance-heartbeat|basic-worker-heartbeat)
          if grep -E '"status":"(ready|ok)"' "$(log_path "$component")" >/dev/null 2>&1 && grep -E '"heartbeat":true' "$(log_path "$component")" >/dev/null 2>&1; then return 0; fi
          health=
          ;;
        *) health= ;;
      esac
      if [ -n "$health" ] && "$CURL_BIN" -fsS --max-time 2 "$health" >/dev/null 2>&1; then return 0; fi
      if [ -z "$health" ] && [ "$strict_akashic_readiness" -eq 0 ] \
        && grep -E '"status":"(ready|ok)"' "$(log_path "$component")" >/dev/null 2>&1; then
        return 0
      fi
      grep -E '"status":"error"' "$(log_path "$component")" >/dev/null 2>&1 && return 1
    fi
    i=$((i + 1))
    sleep .1
  done
  return 1
}

write_identity() {
  component=$1
  pid=$2
  command=$3
  start=$4
  target=$(id_path "$component")
  tmp="$target.tmp.$$"
  {
    printf 'pid=%s\n' "$pid"
    printf 'run_id=%s\n' "$RUN_ID"
    printf 'workspace_id=%s\n' "$WORKSPACE_ID"
    printf 'component=%s\n' "$component"
    printf 'marker=sidera-r6-basic:%s:%s\n' "$RUN_ID" "$component"
    printf 'process_start=%s\n' "$start"
    printf 'argv_hash=%s\n' "$(hash_argv "$command")"
    printf 'command=%s\n' "$command"
  } > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$target"
}

discard_failed_start() {
  component=$1
  failed_pid=$2
  if alive "$failed_pid"; then
    signal_owned TERM "$failed_pid" >/dev/null 2>&1 || true
    attempts=0
    while alive "$failed_pid" && [ "$attempts" -lt 30 ]; do
      sleep .1
      attempts=$((attempts + 1))
    done
    if alive "$failed_pid"; then
      signal_owned KILL "$failed_pid" >/dev/null 2>&1 || true
      sleep .1
    fi
  fi
  rm -f "$(pid_path "$component")" "$(id_path "$component")" "$(env_path "$component")"
}

clear_stopped_receipt() {
  rm -f "$RUNTIME/stopped-receipt.json"
}

record_stopped_identity() {
  component=$1
  "$PYTHON_BIN" - "$RUNTIME/stopped-receipt.json" "$(id_path "$component")" "$RUN_ID" "$WORKSPACE_ID" "$component" <<'PY'
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

receipt_path = Path(sys.argv[1])
identity_path = Path(sys.argv[2])
run_id, workspace_id, component = sys.argv[3:]
values = {}
for line in identity_path.read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if not separator or key in values:
        raise SystemExit("invalid writer identity")
    values[key] = value
if values.get("run_id") != run_id or values.get("workspace_id") != workspace_id or values.get("component") != component:
    raise SystemExit("writer identity mismatch")
try:
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
except FileNotFoundError:
    payload = {"schema_version": 1, "run_id": run_id, "workspace_id": workspace_id, "components": {}}
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit("stopped receipt is invalid") from exc
if not isinstance(payload, dict) or payload.get("run_id") != run_id or payload.get("workspace_id") != workspace_id:
    raise SystemExit("stopped receipt identity mismatch")
components = payload.setdefault("components", {})
if not isinstance(components, dict):
    raise SystemExit("stopped receipt components are invalid")
components[component] = {
    "pid": values["pid"],
    "process_start": values["process_start"],
    "argv_hash": values["argv_hash"],
    "component": component,
    "exit_observed": True,
    "observed_at": datetime.now(timezone.utc).isoformat(),
}
receipt_path.parent.mkdir(parents=True, exist_ok=True)
descriptor, temporary_name = tempfile.mkstemp(prefix=f".{receipt_path.name}.", dir=receipt_path.parent, text=True)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary_name, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary_name, receipt_path)
finally:
    try:
        Path(temporary_name).unlink()
    except OSError:
        pass
PY
  chmod 600 "$RUNTIME/stopped-receipt.json"
}

start_one() {
  component=$1
  if [ "$BASIC_MODE" = interactive ] && [ "$component" = basic-acceptance-heartbeat ]; then
    printf '%s: unavailable in interactive mode\n' "$component" >&2
    return 64
  fi
  if [ "$BASIC_MODE" = acceptance ] && [ "$component" = basic-worker-heartbeat ]; then
    printf '%s: unavailable in acceptance mode\n' "$component" >&2
    return 64
  fi
  clear_stopped_receipt
  if [ "$READ_ONLY_MODE" -eq 1 ]; then
    case " $READ_ONLY_COMPONENTS " in
      *" $component "*) ;;
      *) printf '%s: refused in restore read-only mode\n' "$component" >&2; return 64 ;;
    esac
  fi
  trusted "$component" && return 2
  state_present "$component" && quarantine "$component"
  if occupied "$component"; then
    printf '%s: port is occupied outside this manifest\n' "$component" >&2
    return 1
  fi
  if [ "$component" = akashic-acceptance ]; then
    write_akashic_bridge_config || return
    sync_akashic_acceptance_credential || return
  fi
  pid=$(start_command "$component") || return 1
  printf '%s\n' "$pid" > "$(pid_path "$component")"
  chmod 600 "$(pid_path "$component")"
  i=0
  command=
  while [ "$i" -lt 30 ]; do
    command=$(ps_field "$pid" command= 2>/dev/null || true)
    case "$command" in *"$(signature "$component")"*) break;; esac
    sleep .05
    i=$((i + 1))
  done
  [ -n "$command" ] && alive "$pid" || { discard_failed_start "$component" "$pid"; return 1; }
  start=$(ps_field "$pid" lstart= 2>/dev/null || true)
  [ -n "$start" ] || { discard_failed_start "$component" "$pid"; return 1; }
  ready "$component" || { discard_failed_start "$component" "$pid"; return 1; }
  command=$(ps_field "$pid" command= 2>/dev/null || true)
  [ -n "$command" ] || { discard_failed_start "$component" "$pid"; return 1; }
  write_identity "$component" "$pid" "$command" "$start"
  printf '%s: running/ready\n' "$component"
}

stop_one() {
  component=$1
  state_present "$component" || { printf '%s: stopped\n' "$component"; return 0; }
  trusted "$component" || { printf '%s: refused untrusted process state\n' "$component" >&2; return 1; }
  pid=$(cat "$(pid_path "$component")")
  signal_owned TERM "$pid" || { printf '%s: TERM failed\n' "$component" >&2; return 1; }
  i=0
  while alive "$pid" && [ "$i" -lt 30 ]; do sleep .1; i=$((i + 1)); done
  if alive "$pid"; then
    trusted "$component" || { printf '%s: identity changed before escalation\n' "$component" >&2; return 1; }
    signal_owned KILL "$pid" || return 1
    sleep .1
  fi
  alive "$pid" && { printf '%s: stop failed\n' "$component" >&2; return 1; }
  record_stopped_identity "$component"
  rm -f "$(pid_path "$component")" "$(id_path "$component")" "$(env_path "$component")"
  printf '%s: stopped\n' "$component"
}

prepare() {
  [ "$READ_ONLY_MODE" -eq 1 ] && return 0
  [ "$BASIC_MODE" = interactive ] && return 0
  [ "${SIDERA_R6_BASIC_TEST_MODE:-0}" = 1 ] && return 0
  run_env prepare "$UV_BIN" run --project "$CP_ROOT" alembic -c "$CP_ROOT/alembic.ini" upgrade head
  run_env prepare "$UV_BIN" run --project "$KS_ROOT" alembic -c "$KS_ROOT/alembic.ini" upgrade head
  run_env prepare "$UV_BIN" run --project "$CP_ROOT" python -m sidera_control_plane.bootstrap_basic_workspace --manifest "$MANIFEST" --state-root "$RUN_ROOT/bootstrap-state" --check
  run_env prepare "$UV_BIN" run --project "$CP_ROOT" python -m sidera_control_plane.bootstrap_basic_workspace --manifest "$MANIFEST" --state-root "$RUN_ROOT/bootstrap-state" --apply
}

validate_acceptance_guard() {
  [ "$BASIC_MODE" = interactive ] && return 0
  schema_version=$($PYTHON_BIN - "$MANIFEST" <<'PY'
import json
import sys
from pathlib import Path

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get(
        "schema_version", 1
    )
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit("manifest schema version unavailable") from exc
print(value)
PY
  )
  [ "$schema_version" = 2 ] || return 0
  "$UV_BIN" run --project "$CP_ROOT" --frozen python - "$MANIFEST" <<'PY'
import sys
from pathlib import Path

from sidera_control_plane.basic_acceptance_manifest import load_manifest
from sidera_runtime_guard import RuntimeBudgetGuard

manifest_path = Path(sys.argv[1]).expanduser().resolve()
manifest = load_manifest(manifest_path)
if manifest.schema_version != 2:
    raise SystemExit("schema 2 acceptance manifest required")
RuntimeBudgetGuard(manifest_path)
PY
}

baseline() {
  [ "$READ_ONLY_MODE" -eq 1 ] && return 0
  [ "${SIDERA_R6_BASIC_TEST_MODE:-0}" = 1 ] && return 0
  run_env baseline "$UV_BIN" run --project "$CP_ROOT" python -m sidera_control_plane.notification_worker --once --baseline --workspace-id "$WORKSPACE_ID"
}

start_stack() {
  clear_stopped_receipt
  validate_acceptance_guard
  prepare
  if [ -n "$COMPONENT" ]; then
    [ "$COMPONENT" = notification-worker ] && baseline
    start_one "$COMPONENT"
    update_maintenance_writer_state running "$COMPONENT"
    return
  fi
  started=
  any=0
  components_to_start=$ACCEPTANCE_COMPONENTS
  [ "$BASIC_MODE" = interactive ] && components_to_start=$INTERACTIVE_COMPONENTS
  [ "$READ_ONLY_MODE" -eq 1 ] && components_to_start=$READ_ONLY_COMPONENTS
  if [ "$BOOTSTRAP_AKASHIC" -eq 1 ]; then
    components_to_start="knowledge-service control-plane akashic-acceptance main-agent-gateway"
  fi
  if [ "$BOOTSTRAP_AKASHIC" -ne 1 ] \
    && [ "$BASIC_MODE" = acceptance ] \
    && [ "$READ_ONLY_MODE" -eq 0 ] \
    && trusted akashic-acceptance; then
    if ! akashic_bridge_ready >/dev/null 2>&1; then
      printf '%s\n' 'acceptance start refused: run-local Akashic bridge gate is not ready' >&2
      return 1
    fi
  fi
  for component in $components_to_start; do
    if trusted "$component"; then continue; fi
    if start_one "$component"; then
      :
    else
      code=$?
      for old in $started; do stop_one "$old" >/dev/null 2>&1 || true; done
      return "$code"
    fi
    any=1
    [ "$component" = control-plane ] && baseline
    started="$started $component"
  done
  [ "$any" -eq 1 ] || { printf 'stack already running for run %s\n' "$RUN_ID" >&2; return 2; }
  update_maintenance_writer_state running $components_to_start
}

status_one() {
  component=$1
  if trusted "$component"; then
    if ready "$component"; then
      printf '%s: running/ready\n' "$component"
    else
      printf '%s: running/unready\n' "$component"
      return 1
    fi
  elif state_present "$component"; then
    printf '%s: stale/untrusted\n' "$component"
    return 1
  else
    printf '%s: stopped\n' "$component"
  fi
}

stop_stack() {
  status=0
  list=$COMPONENTS
  [ -n "$COMPONENT" ] && list=$COMPONENT
  reverse=
  for component in $list; do reverse="$component $reverse"; done
  for component in $reverse; do
    stop_one "$component" || status=1
  done
  if [ "$status" -eq 0 ]; then
    update_maintenance_writer_state stopped
  else
    update_maintenance_writer_state unknown $list
  fi
  return "$status"
}

case "$ACTION" in
  status)
    status=0
    list=$ACCEPTANCE_COMPONENTS
    [ "$BASIC_MODE" = interactive ] && list=$INTERACTIVE_COMPONENTS
    [ -n "$COMPONENT" ] && list=$COMPONENT
    for component in $list; do status_one "$component" || status=1; done
    exit "$status"
    ;;
  start) start_stack ;;
  stop) stop_stack ;;
restart)
    if [ "$BASIC_MODE" = acceptance ] \
      && [ "$READ_ONLY_MODE" -eq 0 ] \
      && { [ -z "$COMPONENT" ] || [ "$COMPONENT" = akashic-acceptance ]; }; then
      if ! akashic_plugin_gate; then
        printf '%s\n' 'restart refused: run-local Akashic bridge gate is not ready' >&2
        exit 1
      fi
    fi
    [ -n "$COMPONENT" ] && stop_one "$COMPONENT"
    if [ -n "$COMPONENT" ]; then
      validate_acceptance_guard
      start_one "$COMPONENT"
      update_maintenance_writer_state running "$COMPONENT"
    else
      COMPONENT=
      stop_stack
      start_stack
    fi
    ;;
esac
