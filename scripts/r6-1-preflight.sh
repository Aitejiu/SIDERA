#!/bin/bash
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${SIDERA_R6_1_ENV_FILE:-"$REPO_ROOT/.sidera/r6-1.local.env"}
RUNTIME_DIR=${SIDERA_R6_1_RUNTIME_DIR:-"$REPO_ROOT/.sidera/runtime"}

CONTROL_PLANE_HEAD=0018_r6_1_execution_request_lineage
ALLOWED_KEYS='SIDERA_DATABASE_URL|SIDERA_R6_1_TEST_DATABASE_URL|SIDERA_WORKSPACE_ID|SIDERA_CONTROL_PLANE_URL|SIDERA_MAIN_AGENT_GATEWAY_URL|SIDERA_WEB_URL|SIDERA_AKASHIC_ENDPOINT|SIDERA_AKASHIC_WORKSPACE_TOKEN|SIDERA_AKASHIC_WORKSPACE|SIDERA_AKASHIC_CONFIG|SIDERA_OPENCODE_EXECUTABLE|SIDERA_OPENCODE_EXPECTED_VERSION|SIDERA_OPENCODE_MODEL|SIDERA_PLANNING_PROFILE_ID|SIDERA_PLANNING_PROFILE_VERSION'
SEEN_KEYS=''

SIDERA_DATABASE_URL=''
SIDERA_R6_1_TEST_DATABASE_URL=''
SIDERA_WORKSPACE_ID=''
SIDERA_CONTROL_PLANE_URL=''
SIDERA_MAIN_AGENT_GATEWAY_URL=''
SIDERA_WEB_URL=''
SIDERA_AKASHIC_ENDPOINT=''
SIDERA_AKASHIC_WORKSPACE_TOKEN=''
SIDERA_AKASHIC_WORKSPACE=''
SIDERA_AKASHIC_CONFIG=''
SIDERA_OPENCODE_EXECUTABLE=''
SIDERA_OPENCODE_EXPECTED_VERSION=''
SIDERA_OPENCODE_MODEL=''
SIDERA_PLANNING_PROFILE_ID=''
SIDERA_PLANNING_PROFILE_VERSION=''

fail() {
  printf 'r6-1 preflight: %s\n' "$1" >&2
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
    -*)
      usage >&2
      exit 2
      ;;
    *)
      [ "$ENV_FILE" = "${SIDERA_R6_1_ENV_FILE:-$REPO_ROOT/.sidera/r6-1.local.env}" ] \
        || fail "env file was provided more than once"
      ENV_FILE=$1
      shift
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
    SIDERA_DATABASE_URL) SIDERA_DATABASE_URL="$value" ;;
    SIDERA_R6_1_TEST_DATABASE_URL) SIDERA_R6_1_TEST_DATABASE_URL="$value" ;;
    SIDERA_WORKSPACE_ID) SIDERA_WORKSPACE_ID="$value" ;;
    SIDERA_CONTROL_PLANE_URL) SIDERA_CONTROL_PLANE_URL="$value" ;;
    SIDERA_MAIN_AGENT_GATEWAY_URL) SIDERA_MAIN_AGENT_GATEWAY_URL="$value" ;;
    SIDERA_WEB_URL) SIDERA_WEB_URL="$value" ;;
    SIDERA_AKASHIC_ENDPOINT) SIDERA_AKASHIC_ENDPOINT="$value" ;;
    SIDERA_AKASHIC_WORKSPACE_TOKEN) SIDERA_AKASHIC_WORKSPACE_TOKEN="$value" ;;
    SIDERA_AKASHIC_WORKSPACE) SIDERA_AKASHIC_WORKSPACE="$value" ;;
    SIDERA_AKASHIC_CONFIG) SIDERA_AKASHIC_CONFIG="$value" ;;
    SIDERA_OPENCODE_EXECUTABLE) SIDERA_OPENCODE_EXECUTABLE="$value" ;;
    SIDERA_OPENCODE_EXPECTED_VERSION) SIDERA_OPENCODE_EXPECTED_VERSION="$value" ;;
    SIDERA_OPENCODE_MODEL) SIDERA_OPENCODE_MODEL="$value" ;;
    SIDERA_PLANNING_PROFILE_ID) SIDERA_PLANNING_PROFILE_ID="$value" ;;
    SIDERA_PLANNING_PROFILE_VERSION) SIDERA_PLANNING_PROFILE_VERSION="$value" ;;
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
      export\ *|*'$('*|*'`'*|*'$'*|*';'*|*'|'*|*'<'*|*'>'*|*'&'*)
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
  pg_isready -h "$host" -p "$port" >/dev/null 2>&1 \
    || fail "PostgreSQL is not ready for $key"
  PARSED_DB_NAME=$db_name
}

validate_akashic_endpoint() {
  local value=$1 port
  case "$value" in
    /*) ;;
    127.0.0.1:*|localhost:*|\[::1\]:*)
      port=${value##*:}
      case "$port" in
        ''|*[!0-9]*) fail "SIDERA_AKASHIC_ENDPOINT port is invalid" ;;
      esac
      [ "$port" -ge 1 ] && [ "$port" -le 65535 ] \
        || fail "SIDERA_AKASHIC_ENDPOINT port is invalid"
      ;;
    *) fail "SIDERA_AKASHIC_ENDPOINT must be a loopback socket or TCP endpoint" ;;
  esac
}

check_required_commands() {
  local command_name
  for command_name in brew uv node npm curl git pg_isready python3 lsof ps shasum; do
    command -v "$command_name" >/dev/null 2>&1 \
      || fail "required command is missing: $command_name"
  done
  brew --prefix postgresql@16 >/dev/null 2>&1 \
    || fail "Homebrew postgresql@16 is unavailable"
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
    services/main-agent-gateway/pyproject.toml \
    apps/web/package.json \
    integrations/akashic/plugin.py \
    akashic-agent/agent/plugins/base.py; do
    [ -f "$REPO_ROOT/$source_file" ] \
      || fail "required source file is missing: $source_file"
  done
  git -C "$REPO_ROOT" rev-parse HEAD:akashic-agent >/dev/null 2>&1 \
    || fail "Akashic host identity is not readable"
  akashic_status=$(git -C "$REPO_ROOT" status --short -- akashic-agent 2>/dev/null || true)
  [ -z "$akashic_status" ] || fail "Akashic host source has an uncommitted change"
  tracked_paths=$(git -C "$REPO_ROOT" ls-files -- \
    ':(top).sidera' ':(top).sidera/**' ':(top).env' ':(top).env.*' \
    ':(top)references' ':(top)references/**' ':(top).superpowers' \
    ':(top).superpowers/**')
  [ -z "$tracked_paths" ] || fail "machine-local or handoff path is tracked"
}

check_akashic_config() {
  validate_akashic_endpoint "$SIDERA_AKASHIC_ENDPOINT"
  case "$SIDERA_AKASHIC_WORKSPACE" in
    /*) ;;
    *) fail "SIDERA_AKASHIC_WORKSPACE must be an absolute path" ;;
  esac
  case "$SIDERA_AKASHIC_CONFIG" in
    /*) ;;
    *) fail "SIDERA_AKASHIC_CONFIG must be an absolute path" ;;
  esac
  [ -d "$SIDERA_AKASHIC_WORKSPACE" ] \
    || fail "Akashic workspace path is missing"
  [ ! -L "$SIDERA_AKASHIC_WORKSPACE" ] \
    || fail "Akashic workspace must not be a symlink"
  [ -f "$SIDERA_AKASHIC_CONFIG" ] \
    || fail "Akashic config file is missing"
  [ ! -L "$SIDERA_AKASHIC_CONFIG" ] \
    || fail "Akashic config must not be a symlink"
  check_private_file_mode "Akashic config" "$SIDERA_AKASHIC_CONFIG"
  mode=$(stat -f '%Lp' "$SIDERA_AKASHIC_WORKSPACE" 2>/dev/null \
    || stat -c '%a' "$SIDERA_AKASHIC_WORKSPACE" 2>/dev/null || true)
  [ "$mode" = 700 ] || fail "Akashic workspace must have mode 0700"
}

check_opencode() {
  local opencode_version probe_output patch_capability
  case "$SIDERA_OPENCODE_EXECUTABLE" in
    /*) ;;
    *) fail "SIDERA_OPENCODE_EXECUTABLE must be an absolute path" ;;
  esac
  [ -x "$SIDERA_OPENCODE_EXECUTABLE" ] \
    || fail "OpenCode executable is not executable"
  opencode_version=$("$SIDERA_OPENCODE_EXECUTABLE" --version 2>/dev/null || true)
  case "$opencode_version" in
    *"$SIDERA_OPENCODE_EXPECTED_VERSION"*) ;;
    *) fail "OpenCode version does not match expected version" ;;
  esac
  case "$SIDERA_OPENCODE_MODEL" in
    */*[A-Za-z0-9]*) ;;
    *) fail "SIDERA_OPENCODE_MODEL must use provider/model form" ;;
  esac
  case "$SIDERA_OPENCODE_MODEL" in
    *[!A-Za-z0-9._:/-]*) fail "SIDERA_OPENCODE_MODEL contains unsupported characters" ;;
  esac
  probe_output=$( \
    SIDERA_OPENCODE_EXECUTABLE="$SIDERA_OPENCODE_EXECUTABLE" \
    SIDERA_OPENCODE_EXPECTED_VERSION="$SIDERA_OPENCODE_EXPECTED_VERSION" \
    uv run --project "$REPO_ROOT/services/control-plane" \
      python -m sidera_control_plane.opencode_preflight 2>/dev/null || true
  )
  case "$probe_output" in
    *'"status":"ok"'*) ;;
    *) fail "OpenCode ACP probe failed" ;;
  esac
  patch_capability=$(uv run --project "$REPO_ROOT/services/control-plane" \
    python -c 'from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter; print(list(OpenCodeACPAdapter._PROFILE.context_patch_versions))' \
    2>/dev/null || true)
  [ "$patch_capability" = '[]' ] \
    || fail "OpenCode context patch capability must be empty"
}

check_database_identity() {
  validate_database_url SIDERA_DATABASE_URL "$SIDERA_DATABASE_URL"
  CP_DB_NAME=$PARSED_DB_NAME
  validate_database_url SIDERA_R6_1_TEST_DATABASE_URL "$SIDERA_R6_1_TEST_DATABASE_URL"
  CP_TEST_DB_NAME=$PARSED_DB_NAME
  [ "$CP_DB_NAME" != "$CP_TEST_DB_NAME" ] \
    || fail "runtime and test database identities must be distinct"
}

pid_is_descendant_of() {
  local child=$1 ancestor=$2 current
  case "$child" in ''|*[!0-9]*) return 1 ;; esac
  case "$ancestor" in ''|*[!0-9]*) return 1 ;; esac
  [ "$child" = "$ancestor" ] && return 0
  while [ "$child" != "$ancestor" ]; do
    current=$(ps -p "$child" -o ppid= 2>/dev/null || true)
    current=${current//[[:space:]]/}
    case "$current" in
      ''|*[!0-9]*) return 1 ;;
    esac
    case "$current" in
      0|1|"$child") return 1 ;;
    esac
    child=$current
  done
  return 0
}

check_port_identity() {
  local port=$1 component=$2 pid_list pid_file pid port_pid command expected
  local identity identity_pid identity_component identity_repo identity_marker
  local process_start current_process_start
  pid_list=$(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
  [ -z "$pid_list" ] && return 0
  pid_file="$RUNTIME_DIR/pids/$component.pid"
  [ -f "$pid_file" ] || fail "service port is occupied outside SIDERA runtime: $component"
  pid=$(sed -n '1p' "$pid_file" 2>/dev/null || true)
  case "$pid" in
    ''|*[!0-9]*) fail "service port identity is invalid: $component" ;;
  esac
  for port_pid in $pid_list; do
    pid_is_descendant_of "$port_pid" "$pid" \
      || fail "service port is outside the recorded process tree: $component"
  done
  kill -0 "$pid" >/dev/null 2>&1 \
    || fail "service port owner is not alive: $component"
  identity="$RUNTIME_DIR/identity/$component.identity"
  [ -f "$identity" ] || fail "service port runtime identity is missing: $component"
  identity_pid=$(sed -n '1p' "$identity" 2>/dev/null || true)
  identity_component=$(sed -n '2p' "$identity" 2>/dev/null || true)
  identity_repo=$(sed -n '3p' "$identity" 2>/dev/null || true)
  identity_marker=$(sed -n '4p' "$identity" 2>/dev/null || true)
  process_start=$(sed -n '6p' "$identity" 2>/dev/null || true)
  [ "$identity_pid" = "pid=$pid" ] \
    || fail "service port identity PID mismatch: $component"
  [ "$identity_component" = "component=$component" ] \
    || fail "service port identity component mismatch: $component"
  [ "$identity_repo" = "repo=$REPO_ROOT" ] \
    || fail "service port identity repository mismatch: $component"
  case "$identity_marker" in
    "marker=sidera-r6-1:$component"|"marker=sidera-r5-3:$component") ;;
    *) fail "service port identity marker mismatch: $component" ;;
  esac
  [ -n "$process_start" ] || fail "service port identity start time is missing: $component"
  process_start=${process_start#process_start=}
  current_process_start=$(ps -p "$pid" -o lstart= 2>/dev/null || true)
  [ "$current_process_start" = "$process_start" ] \
    || fail "service port process start mismatch: $component"
  command=$(ps -p "$pid" -o args= 2>/dev/null || true)
  case "$component" in
    control-plane) expected='sidera_control_plane.api.app' ;;
    main-agent-gateway) expected='sidera_main_agent_gateway.api.app' ;;
    web) expected='npm' ;;
    *) fail "service port component is unsupported: $component" ;;
  esac
  case "$command" in
    *"$expected"*) ;;
    *) fail "service port process command mismatch: $component" ;;
  esac
}

check_health() {
  local component=$1 url=$2
  curl -fsS --max-time 2 "$url" >/dev/null 2>&1 \
    || fail "$component health check failed"
}

check_migration() {
  local service=$1 env_name=$2 database_url=$3
  local current_file heads_file head_count
  current_file=$(mktemp "${TMPDIR:-/tmp}/sidera-r6-1-current.XXXXXX")
  heads_file=$(mktemp "${TMPDIR:-/tmp}/sidera-r6-1-heads.XXXXXX")
  if ! env SIDERA_DATABASE_URL="$database_url" "$env_name=$database_url" uv run \
    --project "$REPO_ROOT/services/control-plane" \
    alembic -c "$REPO_ROOT/services/control-plane/alembic.ini" current \
    >"$current_file" 2>/dev/null; then
    rm -f "$current_file" "$heads_file"
    fail "$service Alembic current check failed"
  fi
  if ! env SIDERA_DATABASE_URL="$database_url" "$env_name=$database_url" uv run \
    --project "$REPO_ROOT/services/control-plane" \
    alembic -c "$REPO_ROOT/services/control-plane/alembic.ini" heads \
    >"$heads_file" 2>/dev/null; then
    rm -f "$current_file" "$heads_file"
    fail "$service Alembic heads check failed"
  fi
  grep -F "$CONTROL_PLANE_HEAD (head)" "$current_file" >/dev/null 2>&1 \
    || {
      rm -f "$current_file" "$heads_file"
      fail "$service Alembic current does not match expected head"
    }
  grep -F "$CONTROL_PLANE_HEAD (head)" "$heads_file" >/dev/null 2>&1 \
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
  SIDERA_R6_1_TEST_DATABASE_URL \
  SIDERA_WORKSPACE_ID \
  SIDERA_CONTROL_PLANE_URL \
  SIDERA_MAIN_AGENT_GATEWAY_URL \
  SIDERA_WEB_URL \
  SIDERA_AKASHIC_ENDPOINT \
  SIDERA_AKASHIC_WORKSPACE_TOKEN \
  SIDERA_AKASHIC_WORKSPACE \
  SIDERA_AKASHIC_CONFIG \
  SIDERA_OPENCODE_EXECUTABLE \
  SIDERA_OPENCODE_EXPECTED_VERSION \
  SIDERA_OPENCODE_MODEL \
  SIDERA_PLANNING_PROFILE_ID \
  SIDERA_PLANNING_PROFILE_VERSION; do
  case "$required_key" in
    SIDERA_DATABASE_URL) require_value "$required_key" "$SIDERA_DATABASE_URL" ;;
    SIDERA_R6_1_TEST_DATABASE_URL) require_value "$required_key" "$SIDERA_R6_1_TEST_DATABASE_URL" ;;
    SIDERA_WORKSPACE_ID) require_value "$required_key" "$SIDERA_WORKSPACE_ID" ;;
    SIDERA_CONTROL_PLANE_URL) require_value "$required_key" "$SIDERA_CONTROL_PLANE_URL" ;;
    SIDERA_MAIN_AGENT_GATEWAY_URL) require_value "$required_key" "$SIDERA_MAIN_AGENT_GATEWAY_URL" ;;
    SIDERA_WEB_URL) require_value "$required_key" "$SIDERA_WEB_URL" ;;
    SIDERA_AKASHIC_ENDPOINT) require_value "$required_key" "$SIDERA_AKASHIC_ENDPOINT" ;;
    SIDERA_AKASHIC_WORKSPACE_TOKEN) require_value "$required_key" "$SIDERA_AKASHIC_WORKSPACE_TOKEN" ;;
    SIDERA_AKASHIC_WORKSPACE) require_value "$required_key" "$SIDERA_AKASHIC_WORKSPACE" ;;
    SIDERA_AKASHIC_CONFIG) require_value "$required_key" "$SIDERA_AKASHIC_CONFIG" ;;
    SIDERA_OPENCODE_EXECUTABLE) require_value "$required_key" "$SIDERA_OPENCODE_EXECUTABLE" ;;
    SIDERA_OPENCODE_EXPECTED_VERSION) require_value "$required_key" "$SIDERA_OPENCODE_EXPECTED_VERSION" ;;
    SIDERA_OPENCODE_MODEL) require_value "$required_key" "$SIDERA_OPENCODE_MODEL" ;;
    SIDERA_PLANNING_PROFILE_ID) require_value "$required_key" "$SIDERA_PLANNING_PROFILE_ID" ;;
    SIDERA_PLANNING_PROFILE_VERSION) require_value "$required_key" "$SIDERA_PLANNING_PROFILE_VERSION" ;;
  esac
done

validate_uuid SIDERA_WORKSPACE_ID "$SIDERA_WORKSPACE_ID"
validate_uuid SIDERA_PLANNING_PROFILE_ID "$SIDERA_PLANNING_PROFILE_ID"
validate_positive_integer SIDERA_PLANNING_PROFILE_VERSION "$SIDERA_PLANNING_PROFILE_VERSION"
validate_loopback_origin SIDERA_CONTROL_PLANE_URL "$SIDERA_CONTROL_PLANE_URL"
CONTROL_PLANE_PORT=$PARSED_PORT
validate_loopback_origin SIDERA_MAIN_AGENT_GATEWAY_URL "$SIDERA_MAIN_AGENT_GATEWAY_URL"
GATEWAY_PORT=$PARSED_PORT
validate_loopback_origin SIDERA_WEB_URL "$SIDERA_WEB_URL"
WEB_PORT=$PARSED_PORT
[ "$CONTROL_PLANE_PORT" != "$GATEWAY_PORT" ] \
  || fail "control-plane and main-agent-gateway ports must be distinct"
[ "$CONTROL_PLANE_PORT" != "$WEB_PORT" ] \
  || fail "control-plane and web ports must be distinct"
[ "$GATEWAY_PORT" != "$WEB_PORT" ] \
  || fail "main-agent-gateway and web ports must be distinct"

check_database_identity
[ -d "$RUNTIME_DIR" ] || fail "runtime directory is missing"
[ ! -L "$RUNTIME_DIR" ] || fail "runtime directory must not be a symlink"
mode=$(stat -f '%Lp' "$RUNTIME_DIR" 2>/dev/null \
  || stat -c '%a' "$RUNTIME_DIR" 2>/dev/null || true)
[ "$mode" = 700 ] || fail "runtime directory must have mode 0700"
check_required_commands
check_source_identity
check_akashic_config
check_opencode
check_migration "control-plane" SIDERA_DATABASE_URL "$SIDERA_DATABASE_URL"
check_migration "control-plane test database" SIDERA_R6_1_TEST_DATABASE_URL "$SIDERA_R6_1_TEST_DATABASE_URL"
check_port_identity "$CONTROL_PLANE_PORT" control-plane
check_port_identity "$GATEWAY_PORT" main-agent-gateway
check_port_identity "$WEB_PORT" web
check_health control-plane "$SIDERA_CONTROL_PLANE_URL/healthz"
check_health main-agent-gateway "$SIDERA_MAIN_AGENT_GATEWAY_URL/main-agent/v1/health"
check_health web "$SIDERA_WEB_URL/"

OPENAPI_HASH=$(shasum -a 256 "$REPO_ROOT/apps/web/openapi.json" | awk '{print $1}')
case "$OPENAPI_HASH" in
  ????????*) ;;
  *) fail "OpenAPI contract hash could not be calculated" ;;
esac

printf 'r6-1 preflight: ok\n'
printf 'r6-1 preflight: migration %s, OpenCode patch capability empty\n' "$CONTROL_PLANE_HEAD"
printf 'r6-1 preflight: openapi_sha256=%s\n' "$OPENAPI_HASH"
