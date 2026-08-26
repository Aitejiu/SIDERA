#!/bin/bash
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${SIDERA_R5_3_ENV_FILE:-"$REPO_ROOT/.sidera/r5-3.local.env"}
RUNTIME_DIR=${SIDERA_R5_3_RUNTIME_DIR:-"$REPO_ROOT/.sidera/runtime"}

PID_DIR="$RUNTIME_DIR/pids"
LOG_DIR="$RUNTIME_DIR/logs"
IDENTITY_DIR="$RUNTIME_DIR/identity"
LAUNCH_ENV_DIR="$RUNTIME_DIR/launch-env"

KNOWLEDGE_COMPONENT=knowledge-service
CONTROL_COMPONENT=control-plane
CONTEXT_COMPONENT=context-package-worker
CANDIDATE_COMPONENT=knowledge-candidate-worker
PLANNING_COMPONENT=planning-worker
ACTIVATION_COMPONENT=activation-worker
SCHEDULER_COMPONENT=scheduler-worker
GATEWAY_COMPONENT=gateway-worker
MAIN_AGENT_COMPONENT=main-agent-gateway
WEB_COMPONENT=web

STARTED_COMPONENTS=''
LAUNCH_ENV_PATH=''
LAST_LAUNCH_ENV_PATH=''
PYTHON_LAUNCHER_CODE='import os,sys; p=sys.argv[1]; e=dict(line.rstrip("\n").split("=",1) for line in open(p,encoding="utf-8")); os.unlink(p); os.environ.clear(); os.environ.update(e); os.execvp(sys.argv[2],sys.argv[2:])'

fail() {
  printf 'r5-3 stack: %s\n' "$1" >&2
  exit 1
}

usage() {
  printf 'usage: %s start|stop|status|logs <component>|restart <component>\n' "$0" >&2
}

is_component() {
  case "$1" in
    "$KNOWLEDGE_COMPONENT"|"$CONTROL_COMPONENT"|"$CONTEXT_COMPONENT"|"$CANDIDATE_COMPONENT"|"$PLANNING_COMPONENT"|"$ACTIVATION_COMPONENT"|"$SCHEDULER_COMPONENT"|"$GATEWAY_COMPONENT"|"$MAIN_AGENT_COMPONENT"|"$WEB_COMPONENT")
      return 0
      ;;
    *) return 1 ;;
  esac
}

ACTION=${1:-}
COMPONENT=${2:-}
case "$ACTION" in
  start|stop|status)
    [ "$#" -eq 1 ] || { usage; exit 2; }
    ;;
  logs|restart)
    is_component "$COMPONENT" || { usage; exit 2; }
    [ "$#" -eq 2 ] || { usage; exit 2; }
    ;;
  *)
    usage
    exit 2
    ;;
esac

ALLOWED_KEYS='SIDERA_DATABASE_URL|SIDERA_R5_3_TEST_DATABASE_URL|SIDERA_KNOWLEDGE_DATABASE_URL|SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL|SIDERA_WORKSPACE_ID|SIDERA_CONTROL_PLANE_URL|SIDERA_KNOWLEDGE_BASE_URL|SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN|SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN|SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN|SIDERA_MAIN_AGENT_GATEWAY_URL|SIDERA_WEB_URL|SIDERA_CONTEXT_PACKAGE_CUTOVER_AT|SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR|SIDERA_AKASHIC_ENDPOINT|SIDERA_AKASHIC_WORKSPACE_TOKEN|SIDERA_AKASHIC_WORKSPACE|SIDERA_AKASHIC_CONFIG|SIDERA_OPENCODE_EXECUTABLE|SIDERA_OPENCODE_EXPECTED_VERSION|SIDERA_OPENCODE_MODEL|SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS|SIDERA_PLANNING_PROFILE_ID|SIDERA_PLANNING_PROFILE_VERSION'
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
    SIDERA_R5_3_TEST_DATABASE_URL) SIDERA_R5_3_TEST_DATABASE_URL=$value ;;
    SIDERA_KNOWLEDGE_DATABASE_URL) SIDERA_KNOWLEDGE_DATABASE_URL=$value ;;
    SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL) SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL=$value ;;
    SIDERA_WORKSPACE_ID) SIDERA_WORKSPACE_ID=$value ;;
    SIDERA_CONTROL_PLANE_URL) SIDERA_CONTROL_PLANE_URL=$value ;;
    SIDERA_KNOWLEDGE_BASE_URL) SIDERA_KNOWLEDGE_BASE_URL=$value ;;
    SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN) SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN=$value ;;
    SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN) SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN=$value ;;
    SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN) SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN=$value ;;
    SIDERA_MAIN_AGENT_GATEWAY_URL) SIDERA_MAIN_AGENT_GATEWAY_URL=$value ;;
    SIDERA_WEB_URL) SIDERA_WEB_URL=$value ;;
    SIDERA_CONTEXT_PACKAGE_CUTOVER_AT) SIDERA_CONTEXT_PACKAGE_CUTOVER_AT=$value ;;
    SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR) SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR=$value ;;
    SIDERA_AKASHIC_ENDPOINT) SIDERA_AKASHIC_ENDPOINT=$value ;;
    SIDERA_AKASHIC_WORKSPACE_TOKEN) SIDERA_AKASHIC_WORKSPACE_TOKEN=$value ;;
    SIDERA_AKASHIC_WORKSPACE) SIDERA_AKASHIC_WORKSPACE=$value ;;
    SIDERA_AKASHIC_CONFIG) SIDERA_AKASHIC_CONFIG=$value ;;
    SIDERA_OPENCODE_EXECUTABLE) SIDERA_OPENCODE_EXECUTABLE=$value ;;
    SIDERA_OPENCODE_EXPECTED_VERSION) SIDERA_OPENCODE_EXPECTED_VERSION=$value ;;
    SIDERA_OPENCODE_MODEL) SIDERA_OPENCODE_MODEL=$value ;;
    SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS) SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS=$value ;;
    SIDERA_PLANNING_PROFILE_ID) SIDERA_PLANNING_PROFILE_ID=$value ;;
    SIDERA_PLANNING_PROFILE_VERSION) SIDERA_PLANNING_PROFILE_VERSION=$value ;;
  esac
}

load_config() {
  [ -f "$ENV_FILE" ] || fail "env file is missing"
  [ ! -L "$ENV_FILE" ] || fail "env file must not be a symlink"
  local line key value backtick_marker
  backtick_marker=$(printf '\140')
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      *$'\r') line=${line%$'\r'} ;;
    esac
    case "$line" in
      ''|'#'*) continue ;;
      *[![:space:]]*) ;;
      *) continue ;;
    esac
    case "$line" in
      export\ *|*'$('*|*"$backtick_marker"*|*'$'*|*';'*|*'|'*|*'<'*|*'>'*|*'&'*)
        fail "env file contains unsupported shell syntax"
        ;;
    esac
    case "$line" in
      *=*)
        key=${line%%=*}
        value=${line#*=}
        ;;
      *) fail "env file contains a line without KEY=VALUE" ;;
    esac
    case "$key" in
      ''|*[!A-Z0-9_]*|[0-9]*) fail "env file contains an invalid key" ;;
    esac
    is_allowed_key "$key" || fail "env file contains an unknown key"
    case "|$SEEN_KEYS|" in
      *"|$key|"*) fail "env file contains a duplicate key" ;;
    esac
    SEEN_KEYS="$SEEN_KEYS|$key"
    set_config_value "$key" "$value"
  done < "$ENV_FILE"
}

ensure_private_directory() {
  local directory=$1
  mkdir -p "$directory"
  chmod 700 "$directory"
}

prepare_runtime() {
  [ ! -L "$RUNTIME_DIR" ] || fail "runtime directory must not be a symlink"
  ensure_private_directory "$RUNTIME_DIR"
  ensure_private_directory "$PID_DIR"
  ensure_private_directory "$LOG_DIR"
  ensure_private_directory "$IDENTITY_DIR"
  ensure_private_directory "$LAUNCH_ENV_DIR"
  case "$SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR" in
    "$RUNTIME_DIR"/*)
      [ ! -L "$SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR" ] || fail "candidate spool must not be a symlink"
      ensure_private_directory "$SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR"
      ;;
  esac
}

run_preflight() {
  "$SCRIPT_DIR/r5-3-preflight.sh" --env-file "$ENV_FILE" --runtime-dir "$RUNTIME_DIR"
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

read_pid() {
  sed -n '1p' "$(pid_file "$1")"
}

component_expected_command() {
  case "$1" in
    "$KNOWLEDGE_COMPONENT") printf '%s\n' 'sidera_knowledge.api.app' ;;
    "$CONTROL_COMPONENT") printf '%s\n' 'sidera_control_plane.api.app' ;;
    "$CONTEXT_COMPONENT") printf '%s\n' 'sidera_control_plane.context_package_worker' ;;
    "$CANDIDATE_COMPONENT") printf '%s\n' 'sidera_control_plane.knowledge_candidate_worker' ;;
    "$PLANNING_COMPONENT") printf '%s\n' 'sidera_control_plane.planning_worker' ;;
    "$ACTIVATION_COMPONENT") printf '%s\n' 'sidera_control_plane.activation_worker' ;;
    "$SCHEDULER_COMPONENT") printf '%s\n' 'sidera_control_plane.scheduler_worker' ;;
    "$GATEWAY_COMPONENT") printf '%s\n' 'sidera_control_plane.gateway_worker' ;;
    "$MAIN_AGENT_COMPONENT") printf '%s\n' 'sidera_main_agent_gateway.api.app' ;;
    "$WEB_COMPONENT") printf '%s\n' 'run dev' ;;
    *) return 1 ;;
  esac
}

identity_matches_process() {
  local component=$1 pid identity identity_pid identity_component identity_repo identity_marker process_start current_process_start
  [ -f "$(pid_file "$component")" ] || return 1
  pid=$(read_pid "$component" 2>/dev/null || true)
  case "$pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  identity=$(identity_file "$component")
  [ -f "$identity" ] || return 1
  identity_pid=$(sed -n '1p' "$identity" 2>/dev/null || true)
  identity_component=$(sed -n '2p' "$identity" 2>/dev/null || true)
  identity_repo=$(sed -n '3p' "$identity" 2>/dev/null || true)
  identity_marker=$(sed -n '4p' "$identity" 2>/dev/null || true)
  process_start=$(sed -n '6p' "$identity" 2>/dev/null || true)
  [ "$identity_pid" = "pid=$pid" ] || return 1
  [ "$identity_component" = "component=$component" ] || return 1
  [ "$identity_repo" = "repo=$REPO_ROOT" ] || return 1
  [ "$identity_marker" = "marker=sidera-r5-3:$component" ] || return 1
  [ -n "$process_start" ] || return 1
  process_start=${process_start#process_start=}
  current_process_start=$(ps -p "$pid" -o lstart= 2>/dev/null || true)
  [ "$current_process_start" = "$process_start" ] || return 1
  kill -0 "$pid" >/dev/null 2>&1 || return 1
}

pid_is_trusted() {
  local component=$1 pid command expected
  pid=$(read_pid "$component" 2>/dev/null || true)
  case "$pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  identity_matches_process "$component" || return 1
  command=$(ps -p "$pid" -o command= 2>/dev/null || true)
  expected=$(component_expected_command "$component")
  case "$command" in
    *"$expected"*) return 0 ;;
    *) return 1 ;;
  esac
}

quarantine_component() {
  local component=$1 stamp path suffix target
  stamp=$(date +%s)
  path=$(pid_file "$component")
  if [ -f "$path" ]; then
    suffix=0
    target="$path.stale.$stamp"
    while [ -e "$target" ]; do
      suffix=$((suffix + 1))
      target="$path.stale.$stamp.$suffix"
    done
    mv "$path" "$target"
  fi
  path=$(identity_file "$component")
  if [ -f "$path" ]; then
    suffix=0
    target="$path.stale.$stamp"
    while [ -e "$target" ]; do
      suffix=$((suffix + 1))
      target="$path.stale.$stamp.$suffix"
    done
    mv "$path" "$target"
  fi
  printf 'r5-3 stack: quarantined stale identity for %s\n' "$component" >&2
}

existing_state() {
  local component=$1
  if [ ! -f "$(pid_file "$component")" ]; then
    printf 'stopped\n'
  elif pid_is_trusted "$component"; then
    printf 'running\n'
  else
    printf 'stale\n'
  fi
}

write_identity() {
  local component=$1 pid=$2 command_label=$3 process_start=$4 path temp_path
  path=$(identity_file "$component")
  temp_path="$path.tmp.$$"
  umask 077
  printf 'pid=%s\ncomponent=%s\nrepo=%s\nmarker=sidera-r5-3:%s\nstarted_at=%s\nprocess_start=%s\ncommand=%s\n' \
    "$pid" "$component" "$REPO_ROOT" "$component" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$process_start" "$command_label" > "$temp_path"
  chmod 600 "$temp_path"
  mv -f "$temp_path" "$path"
}

write_launch_line() {
  printf '%s=%s\n' "$1" "$2" >> "$LAUNCH_ENV_PATH"
}

write_base_launch_env() {
  : > "$LAUNCH_ENV_PATH"
  chmod 600 "$LAUNCH_ENV_PATH"
  write_launch_line PATH "$PATH"
  write_launch_line HOME "${HOME:-}"
  write_launch_line TMPDIR "${TMPDIR:-/tmp}"
  write_launch_line LANG "${LANG:-C}"
}

write_control_plane_env() {
  write_launch_line SIDERA_DATABASE_URL "$SIDERA_DATABASE_URL"
  write_launch_line SIDERA_WORKSPACE_ID "$SIDERA_WORKSPACE_ID"
  write_launch_line SIDERA_KNOWLEDGE_BASE_URL "$SIDERA_KNOWLEDGE_BASE_URL"
  write_launch_line SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN "$SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN"
  write_launch_line SIDERA_CONTEXT_PACKAGE_CUTOVER_AT "$SIDERA_CONTEXT_PACKAGE_CUTOVER_AT"
  write_launch_line SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR "$SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR"
  write_launch_line SIDERA_WEB_ORIGIN "$SIDERA_WEB_URL"
  write_launch_line SIDERA_PLANNING_OPENCODE_EXECUTABLE "$SIDERA_OPENCODE_EXECUTABLE"
  write_launch_line SIDERA_PLANNING_OPENCODE_EXPECTED_VERSION "$SIDERA_OPENCODE_EXPECTED_VERSION"
  write_launch_line SIDERA_PLANNING_OPENCODE_MODEL "$SIDERA_OPENCODE_MODEL"
  write_launch_line SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS "$SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS"
  write_launch_line SIDERA_OPENCODE_EXECUTABLE "$SIDERA_OPENCODE_EXECUTABLE"
  write_launch_line SIDERA_OPENCODE_EXPECTED_VERSION "$SIDERA_OPENCODE_EXPECTED_VERSION"
}

write_component_env() {
  local component=$1
  LAUNCH_ENV_PATH=$(mktemp "$LAUNCH_ENV_DIR/$component.XXXXXX")
  write_base_launch_env
  case "$component" in
    "$KNOWLEDGE_COMPONENT")
      write_launch_line SIDERA_KNOWLEDGE_DATABASE_URL "$SIDERA_KNOWLEDGE_DATABASE_URL"
      write_launch_line SIDERA_KNOWLEDGE_WORKSPACE_ID "$SIDERA_WORKSPACE_ID"
      write_launch_line SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN "$SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN"
      write_launch_line SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN "$SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN"
      write_launch_line SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN "$SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN"
      write_launch_line SIDERA_KNOWLEDGE_HOST 127.0.0.1
      write_launch_line SIDERA_KNOWLEDGE_PORT "$(url_port "$SIDERA_KNOWLEDGE_BASE_URL")"
      ;;
    "$CONTROL_COMPONENT"|"$CONTEXT_COMPONENT"|"$CANDIDATE_COMPONENT"|"$PLANNING_COMPONENT"|"$ACTIVATION_COMPONENT"|"$SCHEDULER_COMPONENT"|"$GATEWAY_COMPONENT")
      write_control_plane_env
      ;;
    "$MAIN_AGENT_COMPONENT")
      write_launch_line SIDERA_MAIN_AGENT_AKASHIC_ENDPOINT "$SIDERA_AKASHIC_ENDPOINT"
      write_launch_line SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN "$SIDERA_AKASHIC_WORKSPACE_TOKEN"
      write_launch_line SIDERA_MAIN_AGENT_SIDERA_WORKSPACE_ID "$SIDERA_WORKSPACE_ID"
      write_launch_line SIDERA_MAIN_AGENT_WEB_ORIGIN "$SIDERA_WEB_URL"
      write_launch_line SIDERA_MAIN_AGENT_HOST 127.0.0.1
      write_launch_line SIDERA_MAIN_AGENT_PORT "$(url_port "$SIDERA_MAIN_AGENT_GATEWAY_URL")"
      ;;
    "$WEB_COMPONENT")
      write_launch_line SIDERA_WEB_KNOWLEDGE_TOKEN "$SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN"
      ;;
  esac
}

url_port() {
  local value=$1 port
  port=${value##*:}
  port=${port%%/*}
  printf '%s\n' "$port"
}

launch_component() {
  local component=$1 command_label=$2
  shift 2
  write_component_env "$component"
  local launch_env=$LAUNCH_ENV_PATH pid process_start
  local log_path
  LAST_LAUNCH_ENV_PATH=$launch_env
  log_path=$(log_file "$component")
  : > "$log_path"
  chmod 600 "$log_path"
  "$PYTHON3" -c "$PYTHON_LAUNCHER_CODE" "$launch_env" "$@" >> "$log_path" 2>&1 &
  pid=$!
  process_start=$(ps -p "$pid" -o lstart= 2>/dev/null || true)
  printf '%s\n' "$pid" > "$(pid_file "$component")"
  chmod 600 "$(pid_file "$component")"
  write_identity "$component" "$pid" "$command_label" "$process_start"
  STARTED_COMPONENTS="$STARTED_COMPONENTS|$component"
  LAUNCH_ENV_PATH=''
  printf 'r5-3 stack: started %s\n' "$component"
}

component_command() {
  local component=$1
  local knowledge_port control_port gateway_port web_port
  knowledge_port=$(url_port "$SIDERA_KNOWLEDGE_BASE_URL")
  control_port=$(url_port "$SIDERA_CONTROL_PLANE_URL")
  gateway_port=$(url_port "$SIDERA_MAIN_AGENT_GATEWAY_URL")
  web_port=$(url_port "$SIDERA_WEB_URL")
  case "$component" in
    "$KNOWLEDGE_COMPONENT")
      launch_component "$component" 'knowledge-service uvicorn' \
        uv run --project "$REPO_ROOT/services/knowledge-service" uvicorn \
        sidera_knowledge.api.app:app --host 127.0.0.1 --port "$knowledge_port"
      ;;
    "$CONTROL_COMPONENT")
      launch_component "$component" 'control-plane uvicorn' \
        uv run --project "$REPO_ROOT/services/control-plane" uvicorn \
        sidera_control_plane.api.app:create_app --factory --host 127.0.0.1 --port "$control_port"
      ;;
    "$CONTEXT_COMPONENT")
      launch_component "$component" 'context-package-worker' \
        uv run --project "$REPO_ROOT/services/control-plane" python -u -m sidera_control_plane.context_package_worker
      ;;
    "$CANDIDATE_COMPONENT")
      launch_component "$component" 'knowledge-candidate-worker' \
        uv run --project "$REPO_ROOT/services/control-plane" python -u -m sidera_control_plane.knowledge_candidate_worker
      ;;
    "$PLANNING_COMPONENT")
      launch_component "$component" 'planning-worker' \
        uv run --project "$REPO_ROOT/services/control-plane" python -u -m sidera_control_plane.planning_worker
      ;;
    "$ACTIVATION_COMPONENT")
      launch_component "$component" 'activation-worker' \
        uv run --project "$REPO_ROOT/services/control-plane" python -u -m sidera_control_plane.activation_worker
      ;;
    "$SCHEDULER_COMPONENT")
      launch_component "$component" 'scheduler-worker' \
        uv run --project "$REPO_ROOT/services/control-plane" python -u -m sidera_control_plane.scheduler_worker
      ;;
    "$GATEWAY_COMPONENT")
      launch_component "$component" 'gateway-worker' \
        uv run --project "$REPO_ROOT/services/control-plane" python -u -m sidera_control_plane.gateway_worker
      ;;
    "$MAIN_AGENT_COMPONENT")
      launch_component "$component" 'main-agent-gateway uvicorn' \
        uv run --project "$REPO_ROOT/services/main-agent-gateway" uvicorn \
        sidera_main_agent_gateway.api.app:create_app --factory --host 127.0.0.1 --port "$gateway_port"
      ;;
    "$WEB_COMPONENT")
      launch_component "$component" 'web run dev' \
        npm --prefix "$REPO_ROOT/apps/web" run dev -- --host 127.0.0.1 --port "$web_port"
      ;;
    *) return 1 ;;
  esac
}

process_is_alive() {
  pid_is_trusted "$1"
}

worker_is_ready() {
  local component=$1 path
  path=$(log_file "$component")
  [ -s "$path" ] || return 1
  grep -E '"status":"(ok|ready)"' "$path" >/dev/null 2>&1
}

component_health_url() {
  case "$1" in
    "$KNOWLEDGE_COMPONENT") printf '%s/health/ready\n' "$SIDERA_KNOWLEDGE_BASE_URL" ;;
    "$CONTROL_COMPONENT") printf '%s/healthz\n' "$SIDERA_CONTROL_PLANE_URL" ;;
    "$MAIN_AGENT_COMPONENT") printf '%s/main-agent/v1/health\n' "$SIDERA_MAIN_AGENT_GATEWAY_URL" ;;
    "$WEB_COMPONENT") printf '%s/\n' "$SIDERA_WEB_URL" ;;
    *) return 1 ;;
  esac
}

component_is_ready() {
  local component=$1 url response
  case "$component" in
    "$CONTEXT_COMPONENT"|"$CANDIDATE_COMPONENT"|"$PLANNING_COMPONENT"|"$ACTIVATION_COMPONENT"|"$SCHEDULER_COMPONENT"|"$GATEWAY_COMPONENT")
      worker_is_ready "$component"
      ;;
    "$MAIN_AGENT_COMPONENT")
      url=$(component_health_url "$component")
      response=$(curl -fsS --max-time 2 "$url" 2>/dev/null) || return 1
      case "$response" in *'"provider":"available"'*) return 0 ;; *) return 1 ;; esac
      ;;
    *)
      url=$(component_health_url "$component")
      curl -fsS --max-time 2 "$url" >/dev/null 2>&1
      ;;
  esac
}

wait_for_ready() {
  local component=$1 attempt=0
  while [ "$attempt" -lt 120 ]; do
    if process_is_alive "$component" && component_is_ready "$component"; then
      return 0
    fi
    if [ -f "$(pid_file "$component")" ] && ! process_is_alive "$component"; then
      return 1
    fi
    attempt=$((attempt + 1))
    sleep 0.25
  done
  return 1
}

remove_component_state() {
  rm -f "$(pid_file "$1")" "$(identity_file "$1")"
}

stop_component() {
  local component=$1 pid attempt=0
  if [ ! -f "$(pid_file "$component")" ]; then
    rm -f "$(identity_file "$component")"
    printf 'r5-3 stack: %s stopped\n' "$component"
    return 0
  fi
  if ! pid_is_trusted "$component"; then
    quarantine_component "$component"
    return 0
  fi
  pid=$(read_pid "$component")
  kill -TERM "$pid" >/dev/null 2>&1 || true
  while kill -0 "$pid" >/dev/null 2>&1 && [ "$attempt" -lt 100 ]; do
    attempt=$((attempt + 1))
    sleep 0.05
  done
  if kill -0 "$pid" >/dev/null 2>&1; then
    if pid_is_trusted "$component"; then
      kill -KILL "$pid" >/dev/null 2>&1 || true
      attempt=0
      while kill -0 "$pid" >/dev/null 2>&1 && [ "$attempt" -lt 40 ]; do
        attempt=$((attempt + 1))
        sleep 0.05
      done
    fi
  fi
  if kill -0 "$pid" >/dev/null 2>&1; then
    return 1
  fi
  remove_component_state "$component"
  printf 'r5-3 stack: %s stopped\n' "$component"
}

cleanup_failed_component() {
  local component=$1 pid attempt=0
  if [ ! -f "$(pid_file "$component")" ]; then
    rm -f "$(identity_file "$component")"
    return 0
  fi
  pid=$(read_pid "$component" 2>/dev/null || true)
  if identity_matches_process "$component"; then
    kill -TERM "$pid" >/dev/null 2>&1 || true
    while kill -0 "$pid" >/dev/null 2>&1 && [ "$attempt" -lt 100 ]; do
      attempt=$((attempt + 1))
      sleep 0.05
    done
    if kill -0 "$pid" >/dev/null 2>&1 && identity_matches_process "$component"; then
      kill -KILL "$pid" >/dev/null 2>&1 || true
      attempt=0
      while kill -0 "$pid" >/dev/null 2>&1 && [ "$attempt" -lt 40 ]; do
        attempt=$((attempt + 1))
        sleep 0.05
      done
    fi
  fi
  if ! kill -0 "$pid" >/dev/null 2>&1; then
    remove_component_state "$component"
    return 0
  fi
  if pid_is_trusted "$component"; then
    stop_component "$component"
  else
    quarantine_component "$component"
    return 1
  fi
}

rollback_started() {
  local component
  for component in "$WEB_COMPONENT" "$MAIN_AGENT_COMPONENT" "$GATEWAY_COMPONENT" "$SCHEDULER_COMPONENT" "$ACTIVATION_COMPONENT" "$PLANNING_COMPONENT" "$CANDIDATE_COMPONENT" "$CONTEXT_COMPONENT" "$CONTROL_COMPONENT" "$KNOWLEDGE_COMPONENT"; do
    case "$STARTED_COMPONENTS" in
      *"|$component"*) stop_component "$component" >/dev/null 2>&1 || true ;;
    esac
  done
}

start_component() {
  local component=$1 state
  state=$(existing_state "$component")
  case "$state" in
    running)
      wait_for_ready "$component"
      return $?
      ;;
    stale)
      quarantine_component "$component"
      ;;
  esac
  component_command "$component" || return 1
  if wait_for_ready "$component"; then
    return 0
  fi
  if [ -n "$LAST_LAUNCH_ENV_PATH" ]; then
    rm -f "$LAST_LAUNCH_ENV_PATH"
    LAST_LAUNCH_ENV_PATH=''
  fi
  cleanup_failed_component "$component" >/dev/null 2>&1 || true
  return 1
}

start_all() {
  local component
  load_config
  prepare_runtime
  run_preflight >/dev/null
  PYTHON3=$(command -v python3)
  for component in "$KNOWLEDGE_COMPONENT" "$CONTROL_COMPONENT" "$CONTEXT_COMPONENT" "$CANDIDATE_COMPONENT" "$PLANNING_COMPONENT" "$ACTIVATION_COMPONENT" "$SCHEDULER_COMPONENT" "$GATEWAY_COMPONENT" "$MAIN_AGENT_COMPONENT" "$WEB_COMPONENT"; do
    if ! start_component "$component"; then
      rollback_started
      fail "startup failed at $component"
    fi
  done
  printf 'r5-3 stack: all components ready\n'
}

stop_all() {
  local component overall=0
  [ -d "$RUNTIME_DIR" ] || return 0
  for component in "$WEB_COMPONENT" "$MAIN_AGENT_COMPONENT" "$GATEWAY_COMPONENT" "$SCHEDULER_COMPONENT" "$ACTIVATION_COMPONENT" "$PLANNING_COMPONENT" "$CANDIDATE_COMPONENT" "$CONTEXT_COMPONENT" "$CONTROL_COMPONENT" "$KNOWLEDGE_COMPONENT"; do
    if ! stop_component "$component"; then
      overall=1
      printf 'r5-3 stack: failed to stop %s\n' "$component" >&2
    fi
  done
  return "$overall"
}

status_all() {
  local component state status overall=0
  load_config
  for component in "$KNOWLEDGE_COMPONENT" "$CONTROL_COMPONENT" "$CONTEXT_COMPONENT" "$CANDIDATE_COMPONENT" "$PLANNING_COMPONENT" "$ACTIVATION_COMPONENT" "$SCHEDULER_COMPONENT" "$GATEWAY_COMPONENT" "$MAIN_AGENT_COMPONENT" "$WEB_COMPONENT"; do
    state=$(existing_state "$component")
    case "$state" in
      running)
        if component_is_ready "$component"; then
          status='ready'
        else
          status='not-ready'
          overall=1
        fi
        printf '%s running %s\n' "$component" "$status"
        ;;
      stale)
        printf '%s stale not-ready\n' "$component"
        overall=1
        ;;
      *)
        printf '%s stopped not-ready\n' "$component"
        overall=1
        ;;
    esac
  done
  return "$overall"
}

show_logs() {
  load_config
  [ -f "$(log_file "$COMPONENT")" ] || fail "log file is missing: $COMPONENT"
  tail -n 100 "$(log_file "$COMPONENT")"
}

restart_component() {
  load_config
  prepare_runtime
  run_preflight >/dev/null
  PYTHON3=$(command -v python3)
  stop_component "$COMPONENT"
  start_component "$COMPONENT" || fail "restart failed: $COMPONENT"
  printf 'r5-3 stack: restarted %s\n' "$COMPONENT"
}

case "$ACTION" in
  start) start_all ;;
  stop) stop_all ;;
  status) status_all ;;
  logs) show_logs ;;
  restart) restart_component ;;
esac
