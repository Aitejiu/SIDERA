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
COMPONENTS="knowledge-service control-plane akashic-acceptance planning-worker context-package-worker knowledge-candidate-worker activation-worker scheduler-worker gateway-worker plan-transition-worker handoff-worker collaboration-worker notification-worker result-worker main-agent-gateway web"

usage() {
  printf 'usage: %s status|start|stop|restart --manifest PATH [--component NAME]\n' "$0" >&2
}

ACTION=${1:-}
shift || true
MANIFEST=
COMPONENT=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --manifest) [ "$#" -gt 1 ] || { usage; exit 64; }; MANIFEST=$2; shift 2 ;;
    --component) [ "$#" -gt 1 ] || { usage; exit 64; }; COMPONENT=$2; shift 2 ;;
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
PID_DIR="$RUNTIME/pids"
ID_DIR="$RUNTIME/identity"
LOG_DIR="$RUNTIME/logs"
ENV_DIR="$RUNTIME/env"
STALE_DIR="$RUNTIME/stale"
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
    web) printf 'apps/web\n' ;;
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

write_env() {
  component=$1
  target=$(env_path "$component")
  "$PYTHON_BIN" - "$MANIFEST" "$target" "$component" "$RUNTIME" "$CONTROL_URL" "$KNOWLEDGE_URL" "$GATEWAY_URL" "$WEB_URL" "$AKASHIC_URL" <<'PY'
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

manifest_path, target, component, runtime, control_url, knowledge_url, gateway_url, web_url, akashic_url = sys.argv[1:]
manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
env_ref = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
allowed = {"HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TMPDIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "UV_CACHE_DIR"}
selected = set()
for key in ("database_ref", "knowledge_database_ref"):
    value = manifest.get(key)
    if isinstance(value, str) and env_ref.fullmatch(value):
        selected.add(value[4:])
for profile in manifest.get("profiles", []):
    if isinstance(profile, dict):
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

child = {key: value for key, value in os.environ.items() if key in allowed or key in selected}
child["SIDERA_DATABASE_URL"] = resolve(str(manifest["database_ref"]))
child["SIDERA_KNOWLEDGE_DATABASE_URL"] = resolve(str(manifest["knowledge_database_ref"]))
child.update({
    "SIDERA_WORKSPACE_ID": str(manifest["workspace_id"]),
    "SIDERA_CONTROL_PLANE_URL": control_url,
    "SIDERA_KNOWLEDGE_BASE_URL": knowledge_url,
    "SIDERA_MAIN_AGENT_GATEWAY_URL": gateway_url,
    "SIDERA_WEB_URL": web_url,
    "SIDERA_R6_BASIC_RUN_ID": str(manifest["run_id"]),
    "SIDERA_R6_BASIC_WORKSPACE_ID": str(manifest["workspace_id"]),
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
    "SIDERA_LOCAL_GATE_REPOSITORY": str(manifest["repository"]),
    "SIDERA_RESULT_BASE_COMMIT": str(manifest["base_commit"]),
    "SIDERA_MAIN_AGENT_SIDERA_WORKSPACE_ID": str(manifest["workspace_id"]),
    "SIDERA_AKASHIC_URL": akashic_url,
})
if os.environ.get("SIDERA_R6_BASIC_TEST_MODE") == "1":
    for key in ("SIDERA_R6_BASIC_TEST_CAPTURE", "SIDERA_R6_BASIC_STUB_FAIL_COMPONENT", "SIDERA_R6_BASIC_STUB_IGNORE_TERM_COMPONENT"):
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
    akashic-acceptance)
      config=${SIDERA_R6_BASIC_AKASHIC_CONFIG:-"$AKA_ROOT/config.toml"}
      workspace="$RUNTIME/akashic-workspace"
      mkdir -p "$workspace"
      run_component "$component" "$UV_BIN" run --project "$AKA_ROOT" python main.py --config "$config" --workspace "$workspace"
      ;;
    planning-worker|context-package-worker|knowledge-candidate-worker|activation-worker|scheduler-worker|gateway-worker|plan-transition-worker|handoff-worker|collaboration-worker|notification-worker|result-worker)
      module=$(printf '%s' "$component" | tr '-' '_')
      run_component "$component" "$UV_BIN" run --project "$CP_ROOT" python -u -m "sidera_control_plane.$module"
      ;;
    main-agent-gateway) run_component "$component" "$UV_BIN" run --project "$MG_ROOT" uvicorn sidera_main_agent_gateway.api.app:create_app --factory --host 127.0.0.1 --port "$(url_port "$GATEWAY_URL")" ;;
    web) run_component "$component" npm --prefix "$WEB_ROOT" run dev -- --host 127.0.0.1 --port "$(url_port "$WEB_URL")" ;;
    *) return 1 ;;
  esac
}

ready() {
  component=$1
  i=0
  while [ "$i" -lt 100 ]; do
    pid=$(cat "$(pid_path "$component")" 2>/dev/null || printf 0)
    alive "$pid" || return 1
    if [ "${SIDERA_R6_BASIC_TEST_MODE:-0}" = 1 ]; then
      if grep -E '"status":"(ready|ok)"' "$(log_path "$component")" >/dev/null 2>&1 && grep -E '"heartbeat":true|"diagnostic"' "$(log_path "$component")" >/dev/null 2>&1; then return 0; fi
    else
      case "$component" in
        knowledge-service) health="$(component_url "$component")/health/ready" ;;
        control-plane) health="$(component_url "$component")/healthz" ;;
        main-agent-gateway) health="$(component_url "$component")/main-agent/v1/health" ;;
        web) health="$(component_url "$component")/" ;;
        *) health= ;;
      esac
      if [ -n "$health" ] && "$CURL_BIN" -fsS --max-time 2 "$health" >/dev/null 2>&1; then return 0; fi
      if [ -z "$health" ] && grep -E '"status":"(ready|ok)"' "$(log_path "$component")" >/dev/null 2>&1; then return 0; fi
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

start_one() {
  component=$1
  trusted "$component" && return 2
  state_present "$component" && quarantine "$component"
  if occupied "$component"; then
    printf '%s: port is occupied outside this manifest\n' "$component" >&2
    return 1
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
  [ -n "$command" ] && alive "$pid" || { rm -f "$(pid_path "$component")"; return 1; }
  start=$(ps_field "$pid" lstart= 2>/dev/null || true)
  [ -n "$start" ] || { rm -f "$(pid_path "$component")"; return 1; }
  write_identity "$component" "$pid" "$command" "$start"
  ready "$component" || return 1
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
  rm -f "$(pid_path "$component")" "$(id_path "$component")" "$(env_path "$component")"
  printf '%s: stopped\n' "$component"
}

prepare() {
  [ "${SIDERA_R6_BASIC_TEST_MODE:-0}" = 1 ] && return 0
  run_env prepare "$UV_BIN" run --project "$CP_ROOT" alembic -c "$CP_ROOT/alembic.ini" upgrade head
  run_env prepare "$UV_BIN" run --project "$KS_ROOT" alembic -c "$KS_ROOT/alembic.ini" upgrade head
  run_env prepare "$UV_BIN" run --project "$CP_ROOT" python -m sidera_control_plane.bootstrap_basic_workspace --manifest "$MANIFEST" --check
  run_env prepare "$UV_BIN" run --project "$CP_ROOT" python -m sidera_control_plane.bootstrap_basic_workspace --manifest "$MANIFEST" --apply
}

baseline() {
  [ "${SIDERA_R6_BASIC_TEST_MODE:-0}" = 1 ] && return 0
  run_env baseline "$UV_BIN" run --project "$CP_ROOT" python -m sidera_control_plane.notification_worker --once --baseline --workspace-id "$WORKSPACE_ID"
}

start_stack() {
  prepare
  if [ -n "$COMPONENT" ]; then
    [ "$COMPONENT" = notification-worker ] && baseline
    start_one "$COMPONENT"
    return
  fi
  started=
  any=0
  for component in $COMPONENTS; do
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
}

status_one() {
  component=$1
  if trusted "$component"; then
    if grep -E '"status":"(ready|ok)"' "$(log_path "$component")" >/dev/null 2>&1; then printf '%s: running/ready\n' "$component"; else printf '%s: running/unready\n' "$component"; return 1; fi
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
  return "$status"
}

case "$ACTION" in
  status)
    status=0
    list=$COMPONENTS
    [ -n "$COMPONENT" ] && list=$COMPONENT
    for component in $list; do status_one "$component" || status=1; done
    exit "$status"
    ;;
  start) start_stack ;;
  stop) stop_stack ;;
  restart)
    [ -n "$COMPONENT" ] && stop_one "$COMPONENT"
    [ -n "$COMPONENT" ] && start_one "$COMPONENT" || { COMPONENT=; stop_stack; start_stack; }
    ;;
esac
