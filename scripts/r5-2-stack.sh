#!/bin/bash
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${SIDERA_R5_2_ENV_FILE:-"$REPO_ROOT/.sidera/r5-2.local.env"}
RUNTIME_DIR=${SIDERA_R5_2_RUNTIME_DIR:-"$REPO_ROOT/.sidera/runtime"}
PID_DIR="$RUNTIME_DIR/pids"
LOG_DIR="$RUNTIME_DIR/logs"
IDENTITY_DIR="$RUNTIME_DIR/identity"

CONTROL_COMPONENT=control-plane
PLANNING_COMPONENT=planning-worker
GATEWAY_COMPONENT=main-agent-gateway
WEB_COMPONENT=web

fail() {
  printf 'r5-2 stack: %s\n' "$1" >&2
  return 1
}

usage() {
  printf 'usage: %s start|stop|status|logs [control-plane|planning-worker|main-agent-gateway|web]\n' "$0" >&2
}

ACTION=${1:-}
COMPONENT=${2:-}
case "$ACTION" in
  start|stop|status)
    [ "$#" -le 1 ] || { usage; exit 2; }
    ;;
  logs)
    case "$COMPONENT" in
      "$CONTROL_COMPONENT"|"$PLANNING_COMPONENT"|"$GATEWAY_COMPONENT"|"$WEB_COMPONENT") ;;
      *) usage; exit 2 ;;
    esac
    [ "$#" -eq 2 ] || { usage; exit 2; }
    ;;
  *)
    usage
    exit 2
    ;;
esac

ALLOWED_KEYS='SIDERA_DATABASE_URL|SIDERA_WORKSPACE_ID|SIDERA_PLANNING_PROFILE_ID|SIDERA_PLANNING_PROFILE_VERSION|SIDERA_CONTROL_PLANE_URL|SIDERA_WEB_URL|SIDERA_MAIN_AGENT_GATEWAY_URL|SIDERA_AKASHIC_ENDPOINT|SIDERA_AKASHIC_WORKSPACE_TOKEN|SIDERA_AKASHIC_WORKSPACE|SIDERA_AKASHIC_CONFIG|SIDERA_OPENCODE_EXECUTABLE|SIDERA_OPENCODE_EXPECTED_VERSION|SIDERA_OPENCODE_MODEL'
SEEN_KEYS=''

is_allowed_key() {
  case "|$ALLOWED_KEYS|" in
    *"|$1|"*) return 0 ;;
    *) return 1 ;;
  esac
}

set_config_value() {
  local key=$1 value=$2
  case "$key" in
    SIDERA_DATABASE_URL) SIDERA_DATABASE_URL=$value ;;
    SIDERA_WORKSPACE_ID) SIDERA_WORKSPACE_ID=$value ;;
    SIDERA_PLANNING_PROFILE_ID) SIDERA_PLANNING_PROFILE_ID=$value ;;
    SIDERA_PLANNING_PROFILE_VERSION) SIDERA_PLANNING_PROFILE_VERSION=$value ;;
    SIDERA_CONTROL_PLANE_URL) SIDERA_CONTROL_PLANE_URL=$value ;;
    SIDERA_WEB_URL) SIDERA_WEB_URL=$value ;;
    SIDERA_MAIN_AGENT_GATEWAY_URL) SIDERA_MAIN_AGENT_GATEWAY_URL=$value ;;
    SIDERA_AKASHIC_ENDPOINT) SIDERA_AKASHIC_ENDPOINT=$value ;;
    SIDERA_AKASHIC_WORKSPACE_TOKEN) SIDERA_AKASHIC_WORKSPACE_TOKEN=$value ;;
    SIDERA_AKASHIC_WORKSPACE) SIDERA_AKASHIC_WORKSPACE=$value ;;
    SIDERA_AKASHIC_CONFIG) SIDERA_AKASHIC_CONFIG=$value ;;
    SIDERA_OPENCODE_EXECUTABLE) SIDERA_OPENCODE_EXECUTABLE=$value ;;
    SIDERA_OPENCODE_EXPECTED_VERSION) SIDERA_OPENCODE_EXPECTED_VERSION=$value ;;
    SIDERA_OPENCODE_MODEL) SIDERA_OPENCODE_MODEL=$value ;;
  esac
}

load_config() {
  [ -f "$ENV_FILE" ] || fail "env file is missing"
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
    esac
    case "$line" in
      export\ *|*'$('**|*'`'*|*';'*|*'|'*) fail "env file contains unsupported shell syntax" ;;
    esac
    case "$line" in
      *=*)
        key=${line%%=*}
        value=${line#*=}
        ;;
      *) fail "env file contains a line without KEY=VALUE" ;;
    esac
    is_allowed_key "$key" || fail "env file contains an unknown key"
    case "|$SEEN_KEYS|" in
      *"|$key|"*) fail "env file contains a duplicate key" ;;
    esac
    SEEN_KEYS="$SEEN_KEYS|$key"
    set_config_value "$key" "$value"
  done < "$ENV_FILE"
}

pid_file() {
  printf '%s/%s.pid\n' "$PID_DIR" "$1"
}

identity_file() {
  printf '%s/%s.identity\n' "$IDENTITY_DIR" "$1"
}

log_file() {
  printf '%s/%s.log\n' "$LOG_DIR" "$1"
}

port_from_url() {
  printf '%s\n' "${1##*:}"
}

component_health_url() {
  case "$1" in
    "$CONTROL_COMPONENT") printf '%s/healthz\n' "$SIDERA_CONTROL_PLANE_URL" ;;
    "$GATEWAY_COMPONENT") printf '%s/main-agent/v1/health\n' "$SIDERA_MAIN_AGENT_GATEWAY_URL" ;;
    "$WEB_COMPONENT") printf '%s/\n' "$SIDERA_WEB_URL" ;;
    *) return 1 ;;
  esac
}

component_expected_command() {
  case "$1" in
    "$CONTROL_COMPONENT") printf '%s\n' 'sidera_control_plane.api.app' ;;
    "$PLANNING_COMPONENT") printf '%s\n' 'sidera_control_plane.planning_worker' ;;
    "$GATEWAY_COMPONENT") printf '%s\n' 'sidera_main_agent_gateway.api.app' ;;
    "$WEB_COMPONENT") printf '%s\n' 'run dev' ;;
    *) return 1 ;;
  esac
}

read_pid() {
  local component=$1 path
  path=$(pid_file "$component")
  [ -f "$path" ] || return 1
  sed -n '1p' "$path"
}

pid_is_trusted() {
  local component=$1 pid cmd expected identity repo_line component_line
  pid=$(read_pid "$component" 2>/dev/null || true)
  case "$pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  kill -0 "$pid" >/dev/null 2>&1 || return 1
  identity=$(identity_file "$component")
  [ -f "$identity" ] || return 1
  repo_line=$(sed -n '3p' "$identity")
  component_line=$(sed -n '2p' "$identity")
  [ "$repo_line" = "repo=$REPO_ROOT" ] || return 1
  [ "$component_line" = "component=$component" ] || return 1
  cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
  expected=$(component_expected_command "$component")
  case "$cmd" in
    *"sidera-r5-2:$component"*|*"$expected"*) return 0 ;;
    *) return 1 ;;
  esac
}

quarantine_component() {
  local component=$1 stamp
  stamp=$(date +%s)
  if [ -f "$(pid_file "$component")" ]; then
    mv "$(pid_file "$component")" "$(pid_file "$component").stale.$stamp"
  fi
  if [ -f "$(identity_file "$component")" ]; then
    mv "$(identity_file "$component")" "$(identity_file "$component").stale.$stamp"
  fi
  printf 'r5-2 stack: quarantined stale identity for %s\n' "$component" >&2
}

ensure_runtime_dirs() {
  mkdir -p "$PID_DIR" "$LOG_DIR" "$IDENTITY_DIR"
}

write_identity() {
  local component=$1 pid=$2 command_label=$3 path
  path=$(identity_file "$component")
  printf 'pid=%s\ncomponent=%s\nrepo=%s\nmarker=sidera-r5-2:%s\nstarted_at=%s\ncommand=%s\n' \
    "$pid" "$component" "$REPO_ROOT" "$component" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$command_label" > "$path"
}

existing_state() {
  local component=$1
  if [ ! -f "$(pid_file "$component")" ]; then
    printf 'stopped\n'
    return 0
  fi
  if pid_is_trusted "$component"; then
    printf 'running\n'
    return 0
  fi
  printf 'stale\n'
}

start_process() {
  local component=$1 command_label=$2 log_path pid
  shift 2
  case "$(existing_state "$component")" in
    running)
      printf 'r5-2 stack: %s already running\n' "$component"
      return 0
      ;;
    stale)
      quarantine_component "$component"
      ;;
  esac
  log_path=$(log_file "$component")
  : > "$log_path"
  "$@" >> "$log_path" 2>&1 &
  pid=$!
  printf '%s\n' "$pid" > "$(pid_file "$component")"
  write_identity "$component" "$pid" "$command_label"
  STARTED_COMPONENTS="$STARTED_COMPONENTS|$component"
  printf 'r5-2 stack: started %s\n' "$component"
}

process_is_alive() {
  pid_is_trusted "$1"
}

wait_for_process() {
  local component=$1 attempt
  attempt=0
  while [ "$attempt" -lt 20 ]; do
    process_is_alive "$component" && return 0
    attempt=$((attempt + 1))
    sleep 0.1
  done
  fail "$component exited during startup"
}

wait_for_health() {
  local component=$1 url response attempt
  case "$component" in
    "$PLANNING_COMPONENT")
      wait_for_process "$component"
      return 0
      ;;
  esac
  url=$(component_health_url "$component")
  attempt=0
  while [ "$attempt" -lt 60 ]; do
    if response=$(curl -fsS --max-time 2 "$url" 2>/dev/null); then
      if [ "$component" = "$GATEWAY_COMPONENT" ]; then
        case "$response" in
          *'"provider":"available"'*) return 0 ;;
        esac
      else
        return 0
      fi
    fi
    process_is_alive "$component" || fail "$component exited before health became ready"
    attempt=$((attempt + 1))
    sleep 0.25
  done
  fail "$component health check timed out"
}

stop_component() {
  local component=$1 pid attempt
  if [ ! -f "$(pid_file "$component")" ]; then
    printf 'r5-2 stack: %s stopped\n' "$component"
    return 0
  fi
  if ! pid_is_trusted "$component"; then
    quarantine_component "$component"
    return 1
  fi
  pid=$(read_pid "$component")
  kill -TERM "$pid" >/dev/null 2>&1 || true
  attempt=0
  while kill -0 "$pid" >/dev/null 2>&1 && [ "$attempt" -lt 100 ]; do
    sleep 0.1
    attempt=$((attempt + 1))
  done
  if kill -0 "$pid" >/dev/null 2>&1; then
    if pid_is_trusted "$component"; then
      kill -KILL "$pid" >/dev/null 2>&1 || true
    else
      quarantine_component "$component"
      return 1
    fi
  fi
  rm -f "$(pid_file "$component")" "$(identity_file "$component")"
  printf 'r5-2 stack: %s stopped\n' "$component"
}

all_components_running() {
  pid_is_trusted "$CONTROL_COMPONENT" \
    && pid_is_trusted "$PLANNING_COMPONENT" \
    && pid_is_trusted "$GATEWAY_COMPONENT" \
    && pid_is_trusted "$WEB_COMPONENT"
}

stop_started_components() {
  local component
  for component in "$WEB_COMPONENT" "$GATEWAY_COMPONENT" "$PLANNING_COMPONENT" "$CONTROL_COMPONENT"; do
    case "|$STARTED_COMPONENTS|" in
      *"|$component|"*) stop_component "$component" >/dev/null 2>&1 || true ;;
    esac
  done
}

start_stack() {
  STARTED_COMPONENTS=''
  load_config
  SIDERA_R5_2_ENV_FILE="$ENV_FILE" SIDERA_R5_2_RUNTIME_DIR="$RUNTIME_DIR" \
    "$SCRIPT_DIR/r5-2-preflight.sh"
  ensure_runtime_dirs
  if all_components_running; then
    printf 'r5-2 stack: already running\n'
    return 0
  fi

  uv=$(command -v uv)
  npm=$(command -v npm)
  control_port=$(port_from_url "$SIDERA_CONTROL_PLANE_URL")
  gateway_port=$(port_from_url "$SIDERA_MAIN_AGENT_GATEWAY_URL")
  web_port=$(port_from_url "$SIDERA_WEB_URL")

  if ! env -u SIDERA_AKASHIC_ENDPOINT -u SIDERA_AKASHIC_WORKSPACE_TOKEN \
    -u SIDERA_AKASHIC_WORKSPACE -u SIDERA_AKASHIC_CONFIG \
    SIDERA_DATABASE_URL="$SIDERA_DATABASE_URL" \
    "$uv" run --project "$REPO_ROOT/services/control-plane" alembic \
    -c "$REPO_ROOT/services/control-plane/alembic.ini" upgrade head; then
    fail "database migration failed"
  fi

  if ! start_process "$CONTROL_COMPONENT" 'control-plane uvicorn' \
    env -u SIDERA_AKASHIC_ENDPOINT -u SIDERA_AKASHIC_WORKSPACE_TOKEN \
    -u SIDERA_AKASHIC_WORKSPACE -u SIDERA_AKASHIC_CONFIG \
    SIDERA_DATABASE_URL="$SIDERA_DATABASE_URL" SIDERA_WEB_ORIGIN="$SIDERA_WEB_URL" \
    "$uv" run --project "$REPO_ROOT/services/control-plane" uvicorn \
    sidera_control_plane.api.app:create_app --factory --host 127.0.0.1 --port "$control_port"; then
    stop_started_components
    return 1
  fi
  if ! wait_for_health "$CONTROL_COMPONENT"; then
    stop_started_components
    return 1
  fi

  if ! start_process "$PLANNING_COMPONENT" 'planning-worker module' \
    env -u SIDERA_AKASHIC_ENDPOINT -u SIDERA_AKASHIC_WORKSPACE_TOKEN \
    -u SIDERA_AKASHIC_WORKSPACE -u SIDERA_AKASHIC_CONFIG \
    SIDERA_DATABASE_URL="$SIDERA_DATABASE_URL" \
    "$uv" run --project "$REPO_ROOT/services/control-plane" python \
    -m sidera_control_plane.planning_worker; then
    stop_started_components
    return 1
  fi
  if ! wait_for_health "$PLANNING_COMPONENT"; then
    stop_started_components
    return 1
  fi

  # The gateway receives the only provider credential. Its identity file stores
  # only the fixed command label, never this environment value.
  start_process "$GATEWAY_COMPONENT" 'main-agent-gateway uvicorn' \
    env -u SIDERA_DATABASE_URL -u SIDERA_AKASHIC_CONFIG \
    SIDERA_MAIN_AGENT_AKASHIC_ENDPOINT="$SIDERA_AKASHIC_ENDPOINT" \
    SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN="$SIDERA_AKASHIC_WORKSPACE_TOKEN" \
    SIDERA_MAIN_AGENT_SIDERA_WORKSPACE_ID="$SIDERA_WORKSPACE_ID" \
    SIDERA_MAIN_AGENT_WEB_ORIGIN="$SIDERA_WEB_URL" \
    "$uv" run --project "$REPO_ROOT/services/main-agent-gateway" uvicorn \
    sidera_main_agent_gateway.api.app:create_app --factory --host 127.0.0.1 --port "$gateway_port"
  if ! wait_for_health "$GATEWAY_COMPONENT"; then
    stop_started_components
    return 1
  fi

  if ! start_process "$WEB_COMPONENT" 'web npm run dev' \
    env -u SIDERA_DATABASE_URL -u SIDERA_AKASHIC_ENDPOINT \
    -u SIDERA_AKASHIC_WORKSPACE_TOKEN -u SIDERA_AKASHIC_WORKSPACE \
    -u SIDERA_AKASHIC_CONFIG \
    "$npm" --prefix "$REPO_ROOT/apps/web" run dev -- --host 127.0.0.1 --port "$web_port"; then
    stop_started_components
    return 1
  fi
  if ! wait_for_health "$WEB_COMPONENT"; then
    stop_started_components
    return 1
  fi

  printf 'r5-2 stack: ready\n'
  printf 'control plane: %s\n' "$SIDERA_CONTROL_PLANE_URL"
  printf 'main agent gateway: %s\n' "$SIDERA_MAIN_AGENT_GATEWAY_URL"
  printf 'web: %s\n' "$SIDERA_WEB_URL"
  printf 'logs: %s\n' "$LOG_DIR"
}

status_stack() {
  load_config
  local component state health_url response aggregate=0
  for component in "$CONTROL_COMPONENT" "$PLANNING_COMPONENT" "$GATEWAY_COMPONENT" "$WEB_COMPONENT"; do
    state=$(existing_state "$component")
    case "$state" in
      running)
        case "$component" in
          "$PLANNING_COMPONENT") printf '%s: running\n' "$component" ;;
          *)
            health_url=$(component_health_url "$component")
            if response=$(curl -fsS --max-time 2 "$health_url" 2>/dev/null); then
              printf '%s: running/healthy\n' "$component"
            else
              printf '%s: running/unhealthy\n' "$component"
              aggregate=1
            fi
            ;;
        esac
        ;;
      stopped) printf '%s: stopped\n' "$component" ;;
      stale)
        printf '%s: stale\n' "$component"
        aggregate=1
        ;;
    esac
  done
  return "$aggregate"
}

stop_stack() {
  load_config
  local aggregate=0 component
  for component in "$WEB_COMPONENT" "$GATEWAY_COMPONENT" "$PLANNING_COMPONENT" "$CONTROL_COMPONENT"; do
    stop_component "$component" || aggregate=1
  done
  return "$aggregate"
}

case "$ACTION" in
  start) start_stack ;;
  stop) stop_stack ;;
  status) status_stack ;;
  logs) load_config; tail -n 80 "$(log_file "$COMPONENT")" ;;
esac
