#!/bin/bash
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${SIDERA_R5_2_ENV_FILE:-"$REPO_ROOT/.sidera/r5-2.local.env"}
RUNTIME_DIR=${SIDERA_R5_2_RUNTIME_DIR:-"$REPO_ROOT/.sidera/runtime"}

fail() {
  printf 'r5-2 preflight: %s\n' "$1" >&2
  exit 1
}

usage() {
  printf 'usage: %s [--env-file PATH]\n' "$0"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --env-file)
      [ "$#" -ge 2 ] || fail "--env-file requires a path"
      ENV_FILE=$2
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

[ -f "$ENV_FILE" ] || fail "env file is missing"

ALLOWED_KEYS='SIDERA_DATABASE_URL|SIDERA_R5_2_TEST_DATABASE_URL|SIDERA_WORKSPACE_ID|SIDERA_PLANNING_PROFILE_ID|SIDERA_PLANNING_PROFILE_VERSION|SIDERA_CONTROL_PLANE_URL|SIDERA_WEB_URL|SIDERA_MAIN_AGENT_GATEWAY_URL|SIDERA_AKASHIC_ENDPOINT|SIDERA_AKASHIC_WORKSPACE_TOKEN|SIDERA_AKASHIC_WORKSPACE|SIDERA_AKASHIC_CONFIG|SIDERA_OPENCODE_EXECUTABLE|SIDERA_OPENCODE_EXPECTED_VERSION|SIDERA_OPENCODE_MODEL|SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS'
SEEN_KEYS=''

is_allowed_key() {
  case "|$ALLOWED_KEYS|" in
    *"|$1|"*) return 0 ;;
    *) return 1 ;;
  esac
}

load_env_file() {
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*)
        continue
        ;;
    esac
    case "$line" in
      export\ *|*'$('**|*'`'*|*';'*|*'|'*)
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
    export "$key=$value"
  done < "$ENV_FILE"
}

load_env_file

if [ -n "${SIDERA_AKASHIC_WORKSPACE_TOKEN:-}" ]; then
  mode=$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE" 2>/dev/null || true)
  [ -n "$mode" ] || fail "cannot inspect env file permissions"
  mode=${mode#0}
  mode_num=$((8#$mode))
  [ $((mode_num & 77)) -eq 0 ] || fail "env file must not be group/world readable"
fi

require_value() {
  local key=$1
  eval "[ -n \"\${$key:-}\" ]" || fail "required configuration is missing: $key"
}

for required_key in \
  SIDERA_DATABASE_URL \
  SIDERA_R5_2_TEST_DATABASE_URL \
  SIDERA_WORKSPACE_ID \
  SIDERA_PLANNING_PROFILE_ID \
  SIDERA_PLANNING_PROFILE_VERSION \
  SIDERA_CONTROL_PLANE_URL \
  SIDERA_WEB_URL \
  SIDERA_MAIN_AGENT_GATEWAY_URL \
  SIDERA_AKASHIC_ENDPOINT \
  SIDERA_AKASHIC_WORKSPACE_TOKEN \
  SIDERA_AKASHIC_WORKSPACE \
  SIDERA_AKASHIC_CONFIG \
  SIDERA_OPENCODE_EXECUTABLE \
  SIDERA_OPENCODE_EXPECTED_VERSION \
  SIDERA_OPENCODE_MODEL \
  SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS; do
  require_value "$required_key"
done

validate_uuid() {
  local key=$1 value
  eval "value=\${$key}"
  case "$value" in
    ????????-????-????-????-????????????)
      case "$value" in
        *[!0123456789abcdefABCDEF-]*) fail "$key must be a UUID" ;;
      esac
      ;;
    *)
      fail "$key must be a UUID"
      ;;
  esac
}

validate_positive_integer() {
  local key=$1 value
  eval "value=\${$key}"
  case "$value" in
    ''|*[!0-9]*) fail "$key must be a positive integer" ;;
  esac
  [ "$value" -gt 0 ] || fail "$key must be a positive integer"
}

validate_loopback_url() {
  local key=$1 value port
  eval "value=\${$key}"
  case "$value" in
    http://127.0.0.1:*|http://localhost:*) ;;
    *) fail "$key must use an http loopback URL" ;;
  esac
  port=${value##*:}
  case "$port" in
    ''|*[!0-9]*) fail "$key must use a numeric port" ;;
  esac
  [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || fail "$key port is invalid"
}

validate_uuid SIDERA_WORKSPACE_ID
validate_uuid SIDERA_PLANNING_PROFILE_ID
validate_positive_integer SIDERA_PLANNING_PROFILE_VERSION
validate_positive_integer SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS
validate_loopback_url SIDERA_CONTROL_PLANE_URL
validate_loopback_url SIDERA_WEB_URL
validate_loopback_url SIDERA_MAIN_AGENT_GATEWAY_URL

case "$SIDERA_AKASHIC_ENDPOINT" in
  /*) ;;
  127.0.0.1:*|localhost:*)
    akashic_port=${SIDERA_AKASHIC_ENDPOINT##*:}
    case "$akashic_port" in
      ''|*[!0-9]*) fail "SIDERA_AKASHIC_ENDPOINT port is invalid" ;;
    esac
    ;;
  *) fail "SIDERA_AKASHIC_ENDPOINT must be a loopback socket or TCP endpoint" ;;
esac

for source_file in \
  services/control-plane/pyproject.toml \
  services/control-plane/alembic.ini \
  services/main-agent-gateway/pyproject.toml \
  apps/web/package.json \
  akashic-agent/agent/plugins/base.py; do
  [ -f "$REPO_ROOT/$source_file" ] || fail "required source file is missing: $source_file"
done

for command_name in uv npm curl git pg_isready python3 node; do
  command -v "$command_name" >/dev/null 2>&1 || fail "required command is missing: $command_name"
done

python_version=$(python3 --version 2>&1 || true)
case "$python_version" in
  'Python 3.12.'*|'Python 3.13.'*|'Python 3.14.'*) ;;
  *) fail "Python 3.12 or newer is required" ;;
esac

node_version=$(node --version 2>&1 || true)
case "$node_version" in
  v24.*) ;;
  *) fail "Node 24.x is required" ;;
esac

uv --version >/dev/null 2>&1 || fail "uv is unavailable"
npm --version >/dev/null 2>&1 || fail "npm is unavailable"
curl --version >/dev/null 2>&1 || fail "curl is unavailable"
git --version >/dev/null 2>&1 || fail "git is unavailable"

[ -f "$SIDERA_AKASHIC_CONFIG" ] || fail "Akashic config file is missing"
[ -d "$SIDERA_AKASHIC_WORKSPACE" ] || fail "Akashic workspace path is missing"
[ -x "$SIDERA_OPENCODE_EXECUTABLE" ] || fail "OpenCode executable is not executable"

validate_database_url() {
  local key=$1 value db_without_scheme db_authority db_host_port db_host db_port
  eval "value=\${$key}"
  case "$value" in
    postgresql://*|postgresql+psycopg://*) ;;
    *) fail "$key must use a PostgreSQL URL" ;;
  esac
  db_without_scheme=${value#*://}
  db_authority=${db_without_scheme#*@}
  db_host_port=${db_authority%%/*}
  db_host=${db_host_port%%:*}
  db_port=${db_host_port##*:}
  case "$db_host" in
    127.0.0.1|localhost) ;;
    *) fail "$key must use a loopback host" ;;
  esac
  case "$db_port" in
    ''|*[!0-9]*) fail "$key must include a numeric port" ;;
  esac
  [ "$db_port" -ge 1 ] && [ "$db_port" -le 65535 ] || fail "$key port is invalid"
  pg_isready -h "$db_host" -p "$db_port" >/dev/null 2>&1 || fail "PostgreSQL is not ready"
}

validate_database_url SIDERA_DATABASE_URL
validate_database_url SIDERA_R5_2_TEST_DATABASE_URL

heads_file=$(mktemp "${TMPDIR:-/tmp}/sidera-r5-2-heads.XXXXXX")
trap 'rm -f "$heads_file"' EXIT HUP INT TERM
uv run --project "$REPO_ROOT/services/control-plane" alembic \
  -c "$REPO_ROOT/services/control-plane/alembic.ini" heads >"$heads_file" 2>/dev/null \
  || fail "Alembic heads check failed"
head_count=0
while IFS= read -r head_line || [ -n "$head_line" ]; do
  case "$head_line" in
    *'(head)'*) head_count=$((head_count + 1)) ;;
  esac
done < "$heads_file"
[ "$head_count" -eq 1 ] || fail "Alembic heads diverge"

git -C "$REPO_ROOT" rev-parse HEAD:akashic-agent >/dev/null 2>&1 \
  || fail "Akashic host identity is not readable"
akashic_status=$(git -C "$REPO_ROOT" status --short -- akashic-agent 2>/dev/null || true)
[ -z "$akashic_status" ] || fail "Akashic host source has an uncommitted change"

port_from_url() {
  local key=$1 value
  eval "value=\${$key}"
  printf '%s\n' "${value##*:}"
}

check_port_identity() {
  local port=$1 component=$2 pid_file pid_list pid cmd expected
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi
  pid_list=$(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
  [ -z "$pid_list" ] && return 0
  pid_file="$RUNTIME_DIR/pids/$component.pid"
  [ -f "$pid_file" ] || fail "port $port is occupied outside SIDERA runtime"
  pid=$(sed -n '1p' "$pid_file")
  case "$pid" in
    ''|*[!0-9]*) fail "SIDERA runtime PID file is invalid: $component" ;;
  esac
  kill -0 "$pid" >/dev/null 2>&1 || fail "SIDERA runtime PID is not alive: $component"
  cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
  case "$component" in
    control-plane) expected='sidera_control_plane.api.app' ;;
    main-agent-gateway) expected='sidera_main_agent_gateway.api.app' ;;
    web) expected='run dev' ;;
    *) expected="sidera-r5-2:$component" ;;
  esac
  case "$cmd" in
    *"sidera-r5-2:$component"*|*"$expected"*) ;;
    *) fail "SIDERA runtime PID identity is not trusted: $component" ;;
  esac
}

check_port_identity "$(port_from_url SIDERA_CONTROL_PLANE_URL)" control-plane
check_port_identity "$(port_from_url SIDERA_MAIN_AGENT_GATEWAY_URL)" main-agent-gateway
check_port_identity "$(port_from_url SIDERA_WEB_URL)" web

printf 'r5-2 preflight: ok\n'
