#!/bin/bash
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${SIDERA_R5_3_ENV_FILE:-"$REPO_ROOT/.sidera/r5-3.local.env"}
RUNTIME_DIR=${SIDERA_R5_3_RUNTIME_DIR:-"$REPO_ROOT/.sidera/runtime"}

CONTROL_PLANE_HEAD=0016_r5_3_work_context_packages
KNOWLEDGE_HEAD=0001_knowledge_foundation
ALLOWED_KEYS='SIDERA_DATABASE_URL|SIDERA_R5_3_TEST_DATABASE_URL|SIDERA_KNOWLEDGE_DATABASE_URL|SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL|SIDERA_WORKSPACE_ID|SIDERA_CONTROL_PLANE_URL|SIDERA_KNOWLEDGE_BASE_URL|SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN|SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN|SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN|SIDERA_MAIN_AGENT_GATEWAY_URL|SIDERA_WEB_URL|SIDERA_CONTEXT_PACKAGE_CUTOVER_AT|SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR|SIDERA_AKASHIC_ENDPOINT|SIDERA_AKASHIC_WORKSPACE_TOKEN|SIDERA_AKASHIC_WORKSPACE|SIDERA_AKASHIC_CONFIG|SIDERA_OPENCODE_EXECUTABLE|SIDERA_OPENCODE_EXPECTED_VERSION|SIDERA_OPENCODE_MODEL|SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS|SIDERA_PLANNING_PROFILE_ID|SIDERA_PLANNING_PROFILE_VERSION'
SEEN_KEYS=''

fail() {
  printf 'r5-3 preflight: %s\n' "$1" >&2
  exit 1
}

usage() {
  printf 'usage: %s [--env-file PATH] [--runtime-dir PATH]\n' "$0"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --env-file)
      [ "$#" -ge 2 ] || fail "--env-file requires a path"
      ENV_FILE=$2
      shift 2
      ;;
    --runtime-dir)
      [ "$#" -ge 2 ] || fail "--runtime-dir requires a path"
      RUNTIME_DIR=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

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

check_private_file_mode() {
  local label=$1 file=$2 mode mode_num
  mode=$(stat -f '%Lp' "$file" 2>/dev/null || stat -c '%a' "$file" 2>/dev/null || true)
  case "$mode" in
    ''|*[!0-7]*) fail "cannot inspect permissions for $label" ;;
  esac
  mode_num=$((8#$mode))
  [ $((mode_num & 8#77)) -eq 0 ] || fail "$label must not be group/world readable"
  [ $((mode_num & 8#400)) -ne 0 ] || fail "$label must be readable by its owner"
}

load_env_file() {
  [ -f "$ENV_FILE" ] || fail "env file is missing"
  [ ! -L "$ENV_FILE" ] || fail "env file must not be a symlink"
  check_private_file_mode "env file" "$ENV_FILE"

  local line key value
  local backtick_marker
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
      *)
        fail "env file contains a line without KEY=VALUE"
        ;;
    esac
    case "$key" in
      ''|*[!A-Z0-9_]*|[0-9]*)
        fail "env file contains an invalid key"
        ;;
    esac
    is_allowed_key "$key" || fail "env file contains an unknown key"
    case "|$SEEN_KEYS|" in
      *"|$key|"*) fail "env file contains a duplicate key" ;;
    esac
    SEEN_KEYS="$SEEN_KEYS|$key"
    set_config_value "$key" "$value"
  done < "$ENV_FILE"
}

require_value() {
  local key=$1 value=$2
  [ -n "$value" ] || fail "required configuration is missing: $key"
}

validate_uuid() {
  local key=$1 value=$2
  case "$value" in
    ????????-????-????-????-????????????)
      case "$value" in
        *[!0123456789abcdefABCDEF-]*) fail "$key must be a UUID" ;;
      esac
      ;;
    *) fail "$key must be a UUID" ;;
  esac
}

validate_positive_integer() {
  local key=$1 value=$2
  case "$value" in
    ''|*[!0-9]*) fail "$key must be a positive integer" ;;
  esac
  [ "$value" -gt 0 ] || fail "$key must be a positive integer"
}

validate_loopback_origin() {
  local key=$1 value=$2 port
  case "$value" in
    http://127.0.0.1:*) port=${value#http://127.0.0.1:} ;;
    http://localhost:*) port=${value#http://localhost:} ;;
    http://\[::1\]:*) port=${value#http://\[::1\]:} ;;
    *) fail "$key must use an http loopback origin" ;;
  esac
  case "$port" in
    ''|*[!0-9]*) fail "$key must use a numeric port" ;;
  esac
  [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || fail "$key port is invalid"
  PARSED_PORT=$port
}

validate_cutover_timestamp() {
  local value=$1
  case "$value" in
    ????-??-??T??:??:??Z|????-??-??T??:??:??[+-]??:??) ;;
    *) fail "SIDERA_CONTEXT_PACKAGE_CUTOVER_AT must be timezone-aware ISO-8601" ;;
  esac
  case "$value" in
    *[!0123456789T:+-Z]*) fail "SIDERA_CONTEXT_PACKAGE_CUTOVER_AT must be timezone-aware ISO-8601" ;;
  esac
}

validate_database_url() {
  local key=$1 value=$2 without_scheme authority db_path db_name host_port host port
  case "$value" in
    postgresql://*|postgresql+psycopg://*) ;;
    *) fail "$key must use a PostgreSQL URL" ;;
  esac
  without_scheme=${value#*://}
  case "$without_scheme" in
    */*) ;;
    *) fail "$key must include a database name" ;;
  esac
  authority=${without_scheme%%/*}
  db_path=${without_scheme#*/}
  db_name=${db_path%%\?*}
  db_name=${db_name%%\#*}
  case "$db_name" in
    ''|*[!A-Za-z0-9_-]*) fail "$key database name is invalid" ;;
  esac
  host_port=${authority##*@}
  case "$host_port" in
    \[*\]:*)
      host=${host_port#\[}
      host=${host%%\]*}
      port=${host_port##*\]:}
      ;;
    *:*)
      host=${host_port%%:*}
      port=${host_port##*:}
      ;;
    *) fail "$key must include a host and port" ;;
  esac
  case "$host" in
    127.0.0.1|localhost|::1) ;;
    *) fail "$key must use a loopback host" ;;
  esac
  case "$port" in
    ''|*[!0-9]*) fail "$key must include a numeric port" ;;
  esac
  [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || fail "$key port is invalid"
  pg_isready -h "$host" -p "$port" >/dev/null 2>&1 || fail "PostgreSQL is not ready for $key"
  PARSED_DB_NAME=$db_name
  PARSED_DB_HOST=$host
  PARSED_DB_PORT=$port
}

validate_private_directory() {
  local label=$1 directory=$2 mode mode_num
  [ -d "$directory" ] || fail "$label directory is missing"
  [ ! -L "$directory" ] || fail "$label directory must not be a symlink"
  mode=$(stat -f '%Lp' "$directory" 2>/dev/null || stat -c '%a' "$directory" 2>/dev/null || true)
  case "$mode" in
    ''|*[!0-7]*) fail "cannot inspect permissions for $label directory" ;;
  esac
  mode_num=$((8#$mode))
  [ "$mode_num" -eq $((8#700)) ] || fail "$label directory must have mode 0700"
}

check_runtime_spool() {
  local runtime_real spool_real
  [ -d "$RUNTIME_DIR" ] || fail "runtime directory is missing"
  [ ! -L "$RUNTIME_DIR" ] || fail "runtime directory must not be a symlink"
  runtime_real=$(cd "$RUNTIME_DIR" 2>/dev/null && pwd -P) || fail "runtime directory is unreadable"
  spool_real=$(cd "$SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR" 2>/dev/null && pwd -P) \
    || fail "candidate spool directory is unreadable"
  case "$spool_real" in
    "$runtime_real"/*) ;;
    *) fail "candidate spool must be inside the runtime directory" ;;
  esac
  validate_private_directory "candidate spool" "$SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR"
}

check_required_commands() {
  local command_name
  for command_name in brew uv node npm curl git pg_isready python3 lsof ps; do
    command -v "$command_name" >/dev/null 2>&1 \
      || fail "required command is missing: $command_name"
  done
  brew_prefix=$(brew --prefix postgresql@16 2>/dev/null || true)
  [ -n "$brew_prefix" ] || fail "Homebrew postgresql@16 is unavailable"
  uv --version >/dev/null 2>&1 || fail "uv is unavailable"
  npm --version >/dev/null 2>&1 || fail "npm is unavailable"
  curl --version >/dev/null 2>&1 || fail "curl is unavailable"
  git --version >/dev/null 2>&1 || fail "git is unavailable"
  node_version=$(node --version 2>/dev/null || true)
  case "$node_version" in
    v24.*) ;;
    *) fail "Node 24.x is required" ;;
  esac
  project_python_version=$(uv run --project "$REPO_ROOT/services/control-plane" python --version 2>/dev/null || true)
  case "$project_python_version" in
    'Python 3.12.'*|'Python 3.13.'*|'Python 3.14.'*) ;;
    *) fail "uv project Python 3.12 or newer is required" ;;
  esac
}

check_source_identity() {
  local source_file tracked_paths akashic_status
  for source_file in \
    services/control-plane/pyproject.toml \
    services/control-plane/alembic.ini \
    services/knowledge-service/pyproject.toml \
    services/knowledge-service/alembic.ini \
    services/main-agent-gateway/pyproject.toml \
    apps/web/package.json \
    integrations/akashic/plugin.py \
    akashic-agent/agent/plugins/base.py; do
    [ -f "$REPO_ROOT/$source_file" ] || fail "required source file is missing: $source_file"
  done
  git -C "$REPO_ROOT" rev-parse HEAD:akashic-agent >/dev/null 2>&1 \
    || fail "Akashic host identity is not readable"
  akashic_status=$(git -C "$REPO_ROOT" status --short -- akashic-agent 2>/dev/null || true)
  [ -z "$akashic_status" ] || fail "Akashic host source has an uncommitted change"
  tracked_paths=$(git -C "$REPO_ROOT" ls-files -- \
    '.sidera' '.sidera/**' '.env' '.env.*' 'config/local' 'config/local/**' \
    'references' 'references/**' '.superpowers' '.superpowers/**')
  [ -z "$tracked_paths" ] || fail "machine-local or handoff path is tracked"
}

check_opencode_and_akashic() {
  local opencode_version
  case "$SIDERA_OPENCODE_EXECUTABLE" in
    /*) ;;
    *) fail "SIDERA_OPENCODE_EXECUTABLE must be an absolute path" ;;
  esac
  [ -x "$SIDERA_OPENCODE_EXECUTABLE" ] || fail "OpenCode executable is not executable"
  opencode_version=$("$SIDERA_OPENCODE_EXECUTABLE" --version 2>/dev/null || true)
  case "$opencode_version" in
    *"$SIDERA_OPENCODE_EXPECTED_VERSION"*) ;;
    *) fail "OpenCode version does not match expected version" ;;
  esac
  case "$SIDERA_AKASHIC_WORKSPACE" in
    /*) ;;
    *) fail "SIDERA_AKASHIC_WORKSPACE must be an absolute path" ;;
  esac
  case "$SIDERA_AKASHIC_CONFIG" in
    /*) ;;
    *) fail "SIDERA_AKASHIC_CONFIG must be an absolute path" ;;
  esac
  [ -d "$SIDERA_AKASHIC_WORKSPACE" ] || fail "Akashic workspace path is missing"
  [ ! -L "$SIDERA_AKASHIC_WORKSPACE" ] || fail "Akashic workspace must not be a symlink"
  [ -f "$SIDERA_AKASHIC_CONFIG" ] || fail "Akashic config file is missing"
  [ ! -L "$SIDERA_AKASHIC_CONFIG" ] || fail "Akashic config must not be a symlink"
  [ -r "$SIDERA_AKASHIC_CONFIG" ] || fail "Akashic config file is not readable"
  case "$SIDERA_AKASHIC_ENDPOINT" in
    /*) ;;
    127.0.0.1:*|localhost:*|\[::1\]:*)
      akashic_port=${SIDERA_AKASHIC_ENDPOINT##*:}
      case "$akashic_port" in
        ''|*[!0-9]*) fail "SIDERA_AKASHIC_ENDPOINT port is invalid" ;;
      esac
      [ "$akashic_port" -ge 1 ] && [ "$akashic_port" -le 65535 \
        ] || fail "SIDERA_AKASHIC_ENDPOINT port is invalid"
      ;;
    *) fail "SIDERA_AKASHIC_ENDPOINT must be a loopback socket or TCP endpoint" ;;
  esac
}

check_port_identity() {
  local port=$1 component=$2 health_url pid_file pid_list pid cmd expected identity
  pid_list=$(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
  [ -z "$pid_list" ] && return 0
  pid_file="$RUNTIME_DIR/pids/$component.pid"
  [ -f "$pid_file" ] || fail "port $port is occupied outside SIDERA runtime"
  pid=$(sed -n '1p' "$pid_file")
  case "$pid" in
    ''|*[!0-9]*) fail "SIDERA runtime PID file is invalid: $component" ;;
  esac
  case " $pid_list " in
    *" $pid "*) ;;
    *) fail "port $port is not owned by the recorded $component PID" ;;
  esac
  kill -0 "$pid" >/dev/null 2>&1 || fail "SIDERA runtime PID is not alive: $component"
  identity="$RUNTIME_DIR/identity/$component.identity"
  [ -f "$identity" ] || fail "SIDERA runtime identity is missing: $component"
  grep -F "repo=$REPO_ROOT" "$identity" >/dev/null 2>&1 \
    || fail "SIDERA runtime identity repo mismatch: $component"
  grep -F "component=$component" "$identity" >/dev/null 2>&1 \
    || fail "SIDERA runtime identity component mismatch: $component"
  grep -F "marker=sidera-r5-3:$component" "$identity" >/dev/null 2>&1 \
    || fail "SIDERA runtime identity marker mismatch: $component"
  cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
  case "$component" in
    control-plane) expected='sidera_control_plane.api.app' ;;
    knowledge-service) expected='sidera_knowledge.api.app' ;;
    main-agent-gateway) expected='sidera_main_agent_gateway.api.app' ;;
    web) expected='run dev' ;;
    *) expected="sidera-r5-3:$component" ;;
  esac
  case "$cmd" in
    *"$expected"*) ;;
    *) fail "SIDERA runtime process identity mismatch: $component" ;;
  esac
  case "$component" in
    control-plane) health_url="$SIDERA_CONTROL_PLANE_URL/healthz" ;;
    knowledge-service) health_url="$SIDERA_KNOWLEDGE_BASE_URL/health/ready" ;;
    main-agent-gateway) health_url="$SIDERA_MAIN_AGENT_GATEWAY_URL/main-agent/v1/health" ;;
    web) health_url="$SIDERA_WEB_URL/" ;;
    *) health_url='' ;;
  esac
  [ -n "$health_url" ] || return 0
  curl -fsS --max-time 2 "$health_url" >/dev/null 2>&1 \
    || fail "$component is running but not healthy"
}

check_migration() {
  local service=$1 env_name=$2 database_url=$3 project_dir=$4 ini_file=$5 expected_head=$6
  local current_file heads_file head_count
  current_file=$(mktemp "${TMPDIR:-/tmp}/sidera-r5-3-current.XXXXXX")
  heads_file=$(mktemp "${TMPDIR:-/tmp}/sidera-r5-3-heads.XXXXXX")
  if ! env "$env_name=$database_url" uv run --project "$project_dir" \
    alembic -c "$ini_file" current >"$current_file" 2>/dev/null; then
    rm -f "$current_file" "$heads_file"
    fail "$service Alembic current check failed"
  fi
  if ! env "$env_name=$database_url" uv run --project "$project_dir" \
    alembic -c "$ini_file" heads >"$heads_file" 2>/dev/null; then
    rm -f "$current_file" "$heads_file"
    fail "$service Alembic heads check failed"
  fi
  grep -F "$expected_head (head)" "$current_file" >/dev/null 2>&1 \
    || {
      rm -f "$current_file" "$heads_file"
      fail "$service Alembic current does not match expected head"
    }
  grep -F "$expected_head (head)" "$heads_file" >/dev/null 2>&1 \
    || {
      rm -f "$current_file" "$heads_file"
      fail "$service Alembic heads do not match expected head"
    }
  head_count=$(grep -c '(head)' "$heads_file" 2>/dev/null || true)
  rm -f "$current_file" "$heads_file"
  [ "$head_count" -eq 1 ] || fail "$service Alembic heads diverge"
}

load_env_file

for required_key in \
  SIDERA_DATABASE_URL \
  SIDERA_R5_3_TEST_DATABASE_URL \
  SIDERA_KNOWLEDGE_DATABASE_URL \
  SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL \
  SIDERA_WORKSPACE_ID \
  SIDERA_CONTROL_PLANE_URL \
  SIDERA_KNOWLEDGE_BASE_URL \
  SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN \
  SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN \
  SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN \
  SIDERA_MAIN_AGENT_GATEWAY_URL \
  SIDERA_WEB_URL \
  SIDERA_CONTEXT_PACKAGE_CUTOVER_AT \
  SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR \
  SIDERA_AKASHIC_ENDPOINT \
  SIDERA_AKASHIC_WORKSPACE_TOKEN \
  SIDERA_AKASHIC_WORKSPACE \
  SIDERA_AKASHIC_CONFIG \
  SIDERA_OPENCODE_EXECUTABLE \
  SIDERA_OPENCODE_EXPECTED_VERSION \
  SIDERA_OPENCODE_MODEL \
  SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS \
  SIDERA_PLANNING_PROFILE_ID \
  SIDERA_PLANNING_PROFILE_VERSION; do
  case "$required_key" in
    SIDERA_DATABASE_URL) require_value "$required_key" "${SIDERA_DATABASE_URL:-}" ;;
    SIDERA_R5_3_TEST_DATABASE_URL) require_value "$required_key" "${SIDERA_R5_3_TEST_DATABASE_URL:-}" ;;
    SIDERA_KNOWLEDGE_DATABASE_URL) require_value "$required_key" "${SIDERA_KNOWLEDGE_DATABASE_URL:-}" ;;
    SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL) require_value "$required_key" "${SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL:-}" ;;
    SIDERA_WORKSPACE_ID) require_value "$required_key" "${SIDERA_WORKSPACE_ID:-}" ;;
    SIDERA_CONTROL_PLANE_URL) require_value "$required_key" "${SIDERA_CONTROL_PLANE_URL:-}" ;;
    SIDERA_KNOWLEDGE_BASE_URL) require_value "$required_key" "${SIDERA_KNOWLEDGE_BASE_URL:-}" ;;
    SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN) require_value "$required_key" "${SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN:-}" ;;
    SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN) require_value "$required_key" "${SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN:-}" ;;
    SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN) require_value "$required_key" "${SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN:-}" ;;
    SIDERA_MAIN_AGENT_GATEWAY_URL) require_value "$required_key" "${SIDERA_MAIN_AGENT_GATEWAY_URL:-}" ;;
    SIDERA_WEB_URL) require_value "$required_key" "${SIDERA_WEB_URL:-}" ;;
    SIDERA_CONTEXT_PACKAGE_CUTOVER_AT) require_value "$required_key" "${SIDERA_CONTEXT_PACKAGE_CUTOVER_AT:-}" ;;
    SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR) require_value "$required_key" "${SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR:-}" ;;
    SIDERA_AKASHIC_ENDPOINT) require_value "$required_key" "${SIDERA_AKASHIC_ENDPOINT:-}" ;;
    SIDERA_AKASHIC_WORKSPACE_TOKEN) require_value "$required_key" "${SIDERA_AKASHIC_WORKSPACE_TOKEN:-}" ;;
    SIDERA_AKASHIC_WORKSPACE) require_value "$required_key" "${SIDERA_AKASHIC_WORKSPACE:-}" ;;
    SIDERA_AKASHIC_CONFIG) require_value "$required_key" "${SIDERA_AKASHIC_CONFIG:-}" ;;
    SIDERA_OPENCODE_EXECUTABLE) require_value "$required_key" "${SIDERA_OPENCODE_EXECUTABLE:-}" ;;
    SIDERA_OPENCODE_EXPECTED_VERSION) require_value "$required_key" "${SIDERA_OPENCODE_EXPECTED_VERSION:-}" ;;
    SIDERA_OPENCODE_MODEL) require_value "$required_key" "${SIDERA_OPENCODE_MODEL:-}" ;;
    SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS) require_value "$required_key" "${SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS:-}" ;;
    SIDERA_PLANNING_PROFILE_ID) require_value "$required_key" "${SIDERA_PLANNING_PROFILE_ID:-}" ;;
    SIDERA_PLANNING_PROFILE_VERSION) require_value "$required_key" "${SIDERA_PLANNING_PROFILE_VERSION:-}" ;;
  esac
done

validate_uuid SIDERA_WORKSPACE_ID "$SIDERA_WORKSPACE_ID"
validate_uuid SIDERA_PLANNING_PROFILE_ID "$SIDERA_PLANNING_PROFILE_ID"
validate_positive_integer SIDERA_PLANNING_PROFILE_VERSION "$SIDERA_PLANNING_PROFILE_VERSION"
validate_positive_integer SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS "$SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS"
validate_loopback_origin SIDERA_CONTROL_PLANE_URL "$SIDERA_CONTROL_PLANE_URL"
CONTROL_PLANE_PORT=$PARSED_PORT
validate_loopback_origin SIDERA_KNOWLEDGE_BASE_URL "$SIDERA_KNOWLEDGE_BASE_URL"
KNOWLEDGE_PORT=$PARSED_PORT
validate_loopback_origin SIDERA_MAIN_AGENT_GATEWAY_URL "$SIDERA_MAIN_AGENT_GATEWAY_URL"
GATEWAY_PORT=$PARSED_PORT
validate_loopback_origin SIDERA_WEB_URL "$SIDERA_WEB_URL"
WEB_PORT=$PARSED_PORT
validate_cutover_timestamp "$SIDERA_CONTEXT_PACKAGE_CUTOVER_AT"

port_seen_count=0
for first_port in "$CONTROL_PLANE_PORT" "$KNOWLEDGE_PORT" "$GATEWAY_PORT" "$WEB_PORT"; do
  for second_port in "$CONTROL_PLANE_PORT" "$KNOWLEDGE_PORT" "$GATEWAY_PORT" "$WEB_PORT"; do
    if [ "$first_port" = "$second_port" ]; then
      port_seen_count=$((port_seen_count + 1))
    fi
  done
done
[ "$port_seen_count" -eq 4 ] || fail "service ports must be distinct"

validate_database_url SIDERA_DATABASE_URL "$SIDERA_DATABASE_URL"
CP_DB_NAME=$PARSED_DB_NAME
validate_database_url SIDERA_R5_3_TEST_DATABASE_URL "$SIDERA_R5_3_TEST_DATABASE_URL"
CP_TEST_DB_NAME=$PARSED_DB_NAME
validate_database_url SIDERA_KNOWLEDGE_DATABASE_URL "$SIDERA_KNOWLEDGE_DATABASE_URL"
KNOWLEDGE_DB_NAME=$PARSED_DB_NAME
validate_database_url SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL "$SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL"
KNOWLEDGE_TEST_DB_NAME=$PARSED_DB_NAME
db_seen_count=0
for first_db in "$CP_DB_NAME" "$CP_TEST_DB_NAME" "$KNOWLEDGE_DB_NAME" "$KNOWLEDGE_TEST_DB_NAME"; do
  for second_db in "$CP_DB_NAME" "$CP_TEST_DB_NAME" "$KNOWLEDGE_DB_NAME" "$KNOWLEDGE_TEST_DB_NAME"; do
    if [ "$first_db" = "$second_db" ]; then
      db_seen_count=$((db_seen_count + 1))
    fi
  done
done
[ "$db_seen_count" -eq 4 ] || fail "database identities must be distinct"

check_runtime_spool
check_required_commands
check_source_identity
check_opencode_and_akashic

check_migration "control-plane" SIDERA_DATABASE_URL "$SIDERA_DATABASE_URL" \
  "$REPO_ROOT/services/control-plane" "$REPO_ROOT/services/control-plane/alembic.ini" "$CONTROL_PLANE_HEAD"
check_migration "control-plane test database" SIDERA_DATABASE_URL "$SIDERA_R5_3_TEST_DATABASE_URL" \
  "$REPO_ROOT/services/control-plane" "$REPO_ROOT/services/control-plane/alembic.ini" "$CONTROL_PLANE_HEAD"
check_migration "knowledge-service" SIDERA_KNOWLEDGE_DATABASE_URL "$SIDERA_KNOWLEDGE_DATABASE_URL" \
  "$REPO_ROOT/services/knowledge-service" "$REPO_ROOT/services/knowledge-service/alembic.ini" "$KNOWLEDGE_HEAD"
check_migration "knowledge-service test database" SIDERA_KNOWLEDGE_DATABASE_URL "$SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL" \
  "$REPO_ROOT/services/knowledge-service" "$REPO_ROOT/services/knowledge-service/alembic.ini" "$KNOWLEDGE_HEAD"

check_port_identity "$CONTROL_PLANE_PORT" control-plane
check_port_identity "$KNOWLEDGE_PORT" knowledge-service
check_port_identity "$GATEWAY_PORT" main-agent-gateway
check_port_identity "$WEB_PORT" web

printf 'r5-3 preflight: ok\n'
printf 'r5-3 preflight: four distinct databases, single migration heads, loopback services, redacted secrets\n'
