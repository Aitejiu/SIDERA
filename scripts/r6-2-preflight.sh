#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
DEFAULT_ENV_FILE="$REPO_ROOT/.sidera/r6-2.local.env"
ENV_FILE=${SIDERA_R6_2_ENV_FILE:-$DEFAULT_ENV_FILE}
RUNTIME_DIR=${SIDERA_R6_2_RUNTIME_DIR:-$REPO_ROOT/.sidera/runtime}

CONTROL_PLANE_ROOT="$REPO_ROOT/services/control-plane"
CONTROL_PLANE_BASELINE=0019_r6_2_agent_handoff
CONTROL_PLANE_HEAD=0021_r6_2_retired_handoff_targets
ALLOWED_KEYS='SIDERA_DATABASE_URL|SIDERA_R6_2_TEST_DATABASE_URL|SIDERA_WORKSPACE_ID|SIDERA_CONTROL_PLANE_URL|SIDERA_MAIN_AGENT_GATEWAY_URL|SIDERA_WEB_URL|SIDERA_OPENCODE_EXECUTABLE|SIDERA_OPENCODE_EXPECTED_VERSION|SIDERA_OPENCODE_MODEL|SIDERA_R6_2_REAL_OPENCODE|SIDERA_R6_2_SECOND_ADAPTER|SIDERA_R6_2_SECOND_ADAPTER_TYPE'
SEEN_KEYS=''

SIDERA_DATABASE_URL=''
SIDERA_R6_2_TEST_DATABASE_URL=''
SIDERA_WORKSPACE_ID=''
SIDERA_CONTROL_PLANE_URL=''
SIDERA_MAIN_AGENT_GATEWAY_URL=''
SIDERA_WEB_URL=''
SIDERA_OPENCODE_EXECUTABLE=''
SIDERA_OPENCODE_EXPECTED_VERSION=''
SIDERA_OPENCODE_MODEL=''
SIDERA_R6_2_REAL_OPENCODE=''
SIDERA_R6_2_SECOND_ADAPTER=''
SIDERA_R6_2_SECOND_ADAPTER_TYPE=''

OPENCODE_PREFLIGHT_STATUS=unsupported
OPENCODE_REAL_STATUS=absent
SECOND_ADAPTER_REAL_STATUS=absent
TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r6-2-preflight.XXXXXX")

cleanup() {
  rm -rf "$TEMP_DIR"
}

trap cleanup EXIT HUP INT TERM

fail() {
  printf 'r6-2 preflight: %s\n' "$1" >&2
  printf 'deterministic=blocked\nrelease_classification=blocked\n' >&2
  exit 1
}

usage() {
  printf 'usage: %s [--env-file PATH] [--runtime-dir PATH]\n' "$0"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --env-file)
      [ "$#" -ge 2 ] || fail "--env-file requires a path"
      [ "$ENV_FILE" = "$DEFAULT_ENV_FILE" ] || fail "env file was provided more than once"
      ENV_FILE=$2
      shift 2
      ;;
    --runtime-dir)
      [ "$#" -ge 2 ] || fail "--runtime-dir requires a path"
      [ "$RUNTIME_DIR" = "$REPO_ROOT/.sidera/runtime" ] || fail "runtime directory was provided more than once"
      RUNTIME_DIR=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*|'')
      usage >&2
      exit 2
      ;;
    *)
      [ "$ENV_FILE" = "$DEFAULT_ENV_FILE" ] || fail "env file was provided more than once"
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
    SIDERA_DATABASE_URL) SIDERA_DATABASE_URL=$value ;;
    SIDERA_R6_2_TEST_DATABASE_URL) SIDERA_R6_2_TEST_DATABASE_URL=$value ;;
    SIDERA_WORKSPACE_ID) SIDERA_WORKSPACE_ID=$value ;;
    SIDERA_CONTROL_PLANE_URL) SIDERA_CONTROL_PLANE_URL=$value ;;
    SIDERA_MAIN_AGENT_GATEWAY_URL) SIDERA_MAIN_AGENT_GATEWAY_URL=$value ;;
    SIDERA_WEB_URL) SIDERA_WEB_URL=$value ;;
    SIDERA_OPENCODE_EXECUTABLE) SIDERA_OPENCODE_EXECUTABLE=$value ;;
    SIDERA_OPENCODE_EXPECTED_VERSION) SIDERA_OPENCODE_EXPECTED_VERSION=$value ;;
    SIDERA_OPENCODE_MODEL) SIDERA_OPENCODE_MODEL=$value ;;
    SIDERA_R6_2_REAL_OPENCODE) SIDERA_R6_2_REAL_OPENCODE=$value ;;
    SIDERA_R6_2_SECOND_ADAPTER) SIDERA_R6_2_SECOND_ADAPTER=$value ;;
    SIDERA_R6_2_SECOND_ADAPTER_TYPE) SIDERA_R6_2_SECOND_ADAPTER_TYPE=$value ;;
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

  local line key value backtick
  backtick=$(printf '\140')
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
      export\ *|*'$('|*"$backtick"*|*'$'*|*';'*|*'|'*|*'<'*|*'>'*|*'&'*)
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
  [ -n "$2" ] || fail "required configuration is missing: $1"
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

validate_flag() {
  case "$2" in
    ''|0|1) ;;
    *) fail "$1 must be 0 or 1" ;;
  esac
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

validate_model() {
  case "$2" in
    */*[A-Za-z0-9]*) ;;
    *) fail "$1 must use provider/model form" ;;
  esac
  case "$2" in
    *[!A-Za-z0-9._:/-]*) fail "$1 contains unsupported characters" ;;
  esac
}

check_required_commands() {
  local command_name node_version npm_version project_python_version
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
  npm_version=$(npm --version 2>/dev/null || true)
  case "$npm_version" in
    11.*) ;;
    *) fail "npm 11.x is required" ;;
  esac
  project_python_version=$(uv run --project "$CONTROL_PLANE_ROOT" python --version 2>/dev/null || true)
  case "$project_python_version" in
    'Python 3.12.'*|'Python 3.13.'*|'Python 3.14.'*) ;;
    *) fail "uv project Python 3.12 or newer is required" ;;
  esac
  python3 --version >/dev/null 2>&1 || fail "python3 cannot report its version"
}

check_runtime_directory() {
  local repo_real runtime_real
  case "$RUNTIME_DIR" in
    /*) ;;
    *) fail "runtime directory must be absolute" ;;
  esac
  [ -d "$RUNTIME_DIR" ] || fail "runtime directory is missing"
  [ ! -L "$RUNTIME_DIR" ] || fail "runtime directory must not be a symlink"
  check_private_file_mode "runtime directory" "$RUNTIME_DIR"
  repo_real=$(cd "$REPO_ROOT" && pwd -P)
  runtime_real=$(cd "$RUNTIME_DIR" && pwd -P)
  [ "$runtime_real" != "$repo_real" ] || fail "runtime directory must be below the repository root"
  case "$runtime_real/" in
    "$repo_real/"*) ;;
    *) fail "runtime directory must be below the repository root" ;;
  esac
}

check_source_identity() {
  [ -f "$CONTROL_PLANE_ROOT/pyproject.toml" ] \
    || fail "control-plane project file is missing"
  [ -f "$CONTROL_PLANE_ROOT/alembic.ini" ] \
    || fail "control-plane Alembic configuration is missing"
  [ -f "$REPO_ROOT/apps/web/package.json" ] \
    || fail "Web project file is missing"
  [ -f "$CONTROL_PLANE_ROOT/migrations/versions/$CONTROL_PLANE_BASELINE.py" ] \
    || fail "R6.2 baseline migration is missing"
  [ -f "$CONTROL_PLANE_ROOT/migrations/versions/$CONTROL_PLANE_HEAD.py" ] \
    || fail "current R6.2 migration head is missing"
}

check_database_identity() {
  validate_database_url SIDERA_DATABASE_URL "$SIDERA_DATABASE_URL"
  RUNTIME_DATABASE_NAME=$PARSED_DB_NAME
  validate_database_url SIDERA_R6_2_TEST_DATABASE_URL "$SIDERA_R6_2_TEST_DATABASE_URL"
  TEST_DATABASE_NAME=$PARSED_DB_NAME
  [ "$RUNTIME_DATABASE_NAME" != "$TEST_DATABASE_NAME" ] \
    || fail "runtime and test database identities must be distinct"
}

check_ports() {
  local entry port component owners
  for entry in \
    "$CONTROL_PLANE_PORT control-plane" \
    "$MAIN_AGENT_GATEWAY_PORT main-agent-gateway" \
    "$WEB_PORT web"; do
    port=${entry%% *}
    component=${entry#* }
    owners=$(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
    [ -z "$owners" ] || fail "$component port is occupied; owner was left untouched"
  done
}

check_health() {
  local entry component url
  for entry in \
    "control-plane $SIDERA_CONTROL_PLANE_URL/health" \
    "main-agent-gateway $SIDERA_MAIN_AGENT_GATEWAY_URL/health" \
    "web $SIDERA_WEB_URL"; do
    component=${entry%% *}
    url=${entry#* }
    curl -fsS --max-time 2 "$url" >/dev/null 2>&1 \
      || fail "$component health check failed"
  done
}

safe_env() {
  /usr/bin/env \
    -u DEEPSEEK_API_KEY \
    -u OPENAI_API_KEY \
    -u ANTHROPIC_API_KEY \
    -u OPENCODE_API_KEY \
    -u OPENCODE_CONFIG_CONTENT \
    -u SIDERA_AKASHIC_WORKSPACE_TOKEN \
    -u SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN \
    -u SIDERA_MAIN_AGENT_AKASHIC_WORKSPACE_TOKEN \
    "$@"
}

check_migration() {
  local current_file heads_file head_count
  current_file="$TEMP_DIR/current.txt"
  heads_file="$TEMP_DIR/heads.txt"
  safe_env \
    SIDERA_DATABASE_URL="$SIDERA_R6_2_TEST_DATABASE_URL" \
    SIDERA_R6_2_TEST_DATABASE_URL="$SIDERA_R6_2_TEST_DATABASE_URL" \
    uv run --project "$CONTROL_PLANE_ROOT" alembic \
    -c "$CONTROL_PLANE_ROOT/alembic.ini" upgrade head >/dev/null 2>&1 \
    || fail "disposable test database migration upgrade failed"
  safe_env \
    SIDERA_DATABASE_URL="$SIDERA_R6_2_TEST_DATABASE_URL" \
    uv run --project "$CONTROL_PLANE_ROOT" alembic \
    -c "$CONTROL_PLANE_ROOT/alembic.ini" current >"$current_file" 2>/dev/null \
    || fail "disposable test database Alembic current check failed"
  safe_env \
    SIDERA_DATABASE_URL="$SIDERA_R6_2_TEST_DATABASE_URL" \
    uv run --project "$CONTROL_PLANE_ROOT" alembic \
    -c "$CONTROL_PLANE_ROOT/alembic.ini" heads >"$heads_file" 2>/dev/null \
    || fail "disposable test database Alembic heads check failed"
  grep -F "$CONTROL_PLANE_HEAD (head)" "$current_file" >/dev/null 2>&1 \
    || fail "Alembic current does not match the current R6.2 head"
  grep -F "$CONTROL_PLANE_HEAD (head)" "$heads_file" >/dev/null 2>&1 \
    || fail "Alembic heads do not match the current R6.2 head"
  head_count=$(grep -c '(head)' "$heads_file" 2>/dev/null || true)
  [ "$head_count" -eq 1 ] || fail "Alembic heads are not single-headed"
  grep -F "revision: str = \"$CONTROL_PLANE_HEAD\"" \
    "$CONTROL_PLANE_ROOT/migrations/versions/$CONTROL_PLANE_HEAD.py" >/dev/null 2>&1 \
    || fail "current R6.2 head revision identity is invalid"
  grep -F 'down_revision: str | None = "0020_r6_2_workspace_transfer_outcome"' \
    "$CONTROL_PLANE_ROOT/migrations/versions/$CONTROL_PLANE_HEAD.py" >/dev/null 2>&1 \
    || fail "current R6.2 head ancestry is invalid"
  grep -F 'down_revision: str | None = "0018_r6_1_execution_request_lineage"' \
    "$CONTROL_PLANE_ROOT/migrations/versions/$CONTROL_PLANE_BASELINE.py" >/dev/null 2>&1 \
    || fail "R6.2 baseline migration ancestry is invalid"
  safe_env \
    SIDERA_DATABASE_URL="$SIDERA_R6_2_TEST_DATABASE_URL" \
    SIDERA_R6_2_TEST_DATABASE_URL="$SIDERA_R6_2_TEST_DATABASE_URL" \
    uv run --project "$CONTROL_PLANE_ROOT" pytest -q \
    "$CONTROL_PLANE_ROOT/tests/integration/test_database_schema.py" >/dev/null 2>&1 \
    || fail "disposable test database migration schema test failed"
}

check_opencode_preflight() {
  local opencode_version probe_output
  case "$SIDERA_OPENCODE_EXECUTABLE" in
    /*) ;;
    *) fail "SIDERA_OPENCODE_EXECUTABLE must be an absolute path" ;;
  esac
  [ -x "$SIDERA_OPENCODE_EXECUTABLE" ] \
    || fail "OpenCode executable is not executable"
  opencode_version=$( "$SIDERA_OPENCODE_EXECUTABLE" --version 2>/dev/null || true )
  case "$opencode_version" in
    *"$SIDERA_OPENCODE_EXPECTED_VERSION"*) ;;
    *) fail "OpenCode version does not match expected version" ;;
  esac
  validate_model SIDERA_OPENCODE_MODEL "$SIDERA_OPENCODE_MODEL"
  if ! probe_output=$(safe_env \
    SIDERA_OPENCODE_EXECUTABLE="$SIDERA_OPENCODE_EXECUTABLE" \
    SIDERA_OPENCODE_EXPECTED_VERSION="$SIDERA_OPENCODE_EXPECTED_VERSION" \
    uv run --project "$CONTROL_PLANE_ROOT" \
    python -m sidera_control_plane.opencode_preflight 2>/dev/null); then
    fail "OpenCode ACP preflight command failed"
  fi
  case "$probe_output" in
    *'"status":"ok"'*) ;;
    *) fail "OpenCode ACP preflight did not return ok" ;;
  esac
  case "$probe_output" in
    *'"handoff_status":"supported"'*) OPENCODE_PREFLIGHT_STATUS=supported ;;
    *'"handoff_status":"unsupported"'*) OPENCODE_PREFLIGHT_STATUS=unsupported ;;
    *) fail "OpenCode ACP preflight omitted handoff classification" ;;
  esac
}

check_real_opencode_gate() {
  local gate_output
  [ "$SIDERA_R6_2_REAL_OPENCODE" = 1 ] || return 0
  if [ "$OPENCODE_PREFLIGHT_STATUS" != supported ]; then
    OPENCODE_REAL_STATUS=unsupported
    return 0
  fi
  if gate_output=$(safe_env \
    SIDERA_R6_2_REAL_OPENCODE=1 \
    SIDERA_OPENCODE_EXECUTABLE="$SIDERA_OPENCODE_EXECUTABLE" \
    SIDERA_OPENCODE_EXPECTED_VERSION="$SIDERA_OPENCODE_EXPECTED_VERSION" \
    SIDERA_OPENCODE_MODEL="$SIDERA_OPENCODE_MODEL" \
    uv run --project "$CONTROL_PLANE_ROOT" pytest -q -rs \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_2_real_opencode_handoff.py" \
    2>/dev/null); then
    case "$gate_output" in
      *skipped*) OPENCODE_REAL_STATUS=unsupported ;;
      *'1 passed'*) OPENCODE_REAL_STATUS=pass ;;
      *) OPENCODE_REAL_STATUS=unsupported ;;
    esac
  else
    OPENCODE_REAL_STATUS=unsupported
  fi
}

check_second_adapter_gate() {
  local gate_output
  [ "$SIDERA_R6_2_SECOND_ADAPTER" = 1 ] || return 0
  if gate_output=$(safe_env \
    SIDERA_R6_2_SECOND_ADAPTER=1 \
    SIDERA_R6_2_SECOND_ADAPTER_TYPE="$SIDERA_R6_2_SECOND_ADAPTER_TYPE" \
    uv run --project "$CONTROL_PLANE_ROOT" pytest -q -rs \
    "$CONTROL_PLANE_ROOT/tests/e2e/test_r6_2_second_adapter_handoff.py" \
    2>/dev/null); then
    case "$gate_output" in
      *skipped*) SECOND_ADAPTER_REAL_STATUS=unsupported ;;
      *'1 passed'*) SECOND_ADAPTER_REAL_STATUS=pass ;;
      *) SECOND_ADAPTER_REAL_STATUS=unsupported ;;
    esac
  else
    SECOND_ADAPTER_REAL_STATUS=unsupported
  fi
}

load_env_file

for required_key in \
  SIDERA_DATABASE_URL \
  SIDERA_R6_2_TEST_DATABASE_URL \
  SIDERA_WORKSPACE_ID \
  SIDERA_CONTROL_PLANE_URL \
  SIDERA_MAIN_AGENT_GATEWAY_URL \
  SIDERA_WEB_URL \
  SIDERA_OPENCODE_EXECUTABLE \
  SIDERA_OPENCODE_EXPECTED_VERSION \
  SIDERA_OPENCODE_MODEL; do
  case "$required_key" in
    SIDERA_DATABASE_URL) require_value "$required_key" "$SIDERA_DATABASE_URL" ;;
    SIDERA_R6_2_TEST_DATABASE_URL) require_value "$required_key" "$SIDERA_R6_2_TEST_DATABASE_URL" ;;
    SIDERA_WORKSPACE_ID) require_value "$required_key" "$SIDERA_WORKSPACE_ID" ;;
    SIDERA_CONTROL_PLANE_URL) require_value "$required_key" "$SIDERA_CONTROL_PLANE_URL" ;;
    SIDERA_MAIN_AGENT_GATEWAY_URL) require_value "$required_key" "$SIDERA_MAIN_AGENT_GATEWAY_URL" ;;
    SIDERA_WEB_URL) require_value "$required_key" "$SIDERA_WEB_URL" ;;
    SIDERA_OPENCODE_EXECUTABLE) require_value "$required_key" "$SIDERA_OPENCODE_EXECUTABLE" ;;
    SIDERA_OPENCODE_EXPECTED_VERSION) require_value "$required_key" "$SIDERA_OPENCODE_EXPECTED_VERSION" ;;
    SIDERA_OPENCODE_MODEL) require_value "$required_key" "$SIDERA_OPENCODE_MODEL" ;;
  esac
done

validate_uuid SIDERA_WORKSPACE_ID "$SIDERA_WORKSPACE_ID"
validate_flag SIDERA_R6_2_REAL_OPENCODE "$SIDERA_R6_2_REAL_OPENCODE"
validate_flag SIDERA_R6_2_SECOND_ADAPTER "$SIDERA_R6_2_SECOND_ADAPTER"
case "$SIDERA_R6_2_SECOND_ADAPTER_TYPE" in
  ''|*[!A-Za-z0-9._:-]*) fail "SIDERA_R6_2_SECOND_ADAPTER_TYPE contains unsupported characters" ;;
esac
if [ "$SIDERA_R6_2_SECOND_ADAPTER" = 1 ]; then
  require_value SIDERA_R6_2_SECOND_ADAPTER_TYPE "$SIDERA_R6_2_SECOND_ADAPTER_TYPE"
fi

validate_loopback_origin SIDERA_CONTROL_PLANE_URL "$SIDERA_CONTROL_PLANE_URL"
CONTROL_PLANE_PORT=$PARSED_PORT
validate_loopback_origin SIDERA_MAIN_AGENT_GATEWAY_URL "$SIDERA_MAIN_AGENT_GATEWAY_URL"
MAIN_AGENT_GATEWAY_PORT=$PARSED_PORT
validate_loopback_origin SIDERA_WEB_URL "$SIDERA_WEB_URL"
WEB_PORT=$PARSED_PORT
[ "$CONTROL_PLANE_PORT" != "$MAIN_AGENT_GATEWAY_PORT" ] \
  || fail "control-plane and main-agent-gateway ports must be distinct"
[ "$CONTROL_PLANE_PORT" != "$WEB_PORT" ] \
  || fail "control-plane and web ports must be distinct"
[ "$MAIN_AGENT_GATEWAY_PORT" != "$WEB_PORT" ] \
  || fail "main-agent-gateway and web ports must be distinct"

check_runtime_directory
check_source_identity
check_required_commands
check_database_identity
check_ports
check_health
check_migration
check_opencode_preflight
check_real_opencode_gate
check_second_adapter_gate

if [ "$OPENCODE_REAL_STATUS" = pass ] && [ "$SECOND_ADAPTER_REAL_STATUS" = pass ]; then
  RELEASE_CLASSIFICATION=complete
else
  RELEASE_CLASSIFICATION=partial_gate
fi

printf 'r6-2 preflight: ok\n'
printf 'deterministic=pass\n'
printf 'opencode_preflight=%s\n' "$OPENCODE_PREFLIGHT_STATUS"
printf 'opencode_real=%s\n' "$OPENCODE_REAL_STATUS"
printf 'second_adapter_real=%s\n' "$SECOND_ADAPTER_REAL_STATUS"
printf 'release_classification=%s\n' "$RELEASE_CLASSIFICATION"
