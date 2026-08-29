#!/bin/bash
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r6-1-preflight.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

FAKE_BIN="$TMP/bin"
ENV_FILE="$TMP/r6-1.local.env"
RUNTIME_DIR="$TMP/runtime"
AKASHIC_WORKSPACE="$TMP/akashic-workspace"
AKASHIC_CONFIG="$TMP/akashic-config.toml"
OPENCODE="$FAKE_BIN/opencode"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

make_fake_commands() {
  mkdir -p "$FAKE_BIN" "$RUNTIME_DIR"
  chmod 700 "$RUNTIME_DIR"
  mkdir -p "$AKASHIC_WORKSPACE"
  chmod 700 "$AKASHIC_WORKSPACE"
  printf '%s\n' 'workspace = "test"' > "$AKASHIC_CONFIG"
  chmod 600 "$AKASHIC_CONFIG"

  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--prefix" ]; then printf "/opt/homebrew/opt/postgresql@16\\n"; exit 0; fi' \
    'exit 0' > "$FAKE_BIN/brew"

  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--version" ]; then printf "uv 0.6.0\\n"; exit 0; fi' \
    'case "$*" in' \
    '  *"python --version") printf "Python 3.12.14\\n"; exit 0 ;;' \
    '  *"python -c"*)' \
    '    [ "${R6_1_FAKE_PATCH_CAPABILITY:-0}" = "1" ] && printf "[1]\\n" || printf "[]\\n"' \
    '    exit 0 ;;' \
    "  *opencode_preflight*) printf '%s\\n' '{\"status\":\"ok\"}'; exit 0 ;;" \
    '  *alembic*)' \
    '    [ "${R6_1_FAKE_MIGRATION_FAIL:-0}" = "1" ] && printf "0017_r6_1_plan_revisions (head)\\n" || printf "0018_r6_1_execution_request_lineage (head)\\n"' \
    '    exit 0 ;;' \
    'esac' \
    'exit 0' > "$FAKE_BIN/uv"

  printf '%s\n' \
    '#!/bin/sh' \
    'printf "v24.19.0\\n"' > "$FAKE_BIN/node"
  printf '%s\n' \
    '#!/bin/sh' \
    'printf "11.17.0\\n"' > "$FAKE_BIN/npm"
  printf '%s\n' \
    '#!/bin/sh' \
    'printf "{}\\n"' > "$FAKE_BIN/curl"
  printf '%s\n' \
    '#!/bin/sh' \
    'case "$*" in' \
    '  *"rev-parse HEAD:akashic-agent"*) printf "akashic-test-tree\\n" ;;' \
    'esac' \
    'exit 0' > "$FAKE_BIN/git"
  printf '%s\n' \
    '#!/bin/sh' \
    'case "$*" in' \
    '  *"-iTCP:8000"*) [ "${R6_1_FAKE_PORT_CONFLICT:-0}" = "1" ] && printf "99999\\n" ;;' \
    'esac' > "$FAKE_BIN/lsof"
  printf '%s\n' '#!/bin/sh' 'exit 0' > "$FAKE_BIN/pg_isready"
  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--version" ]; then printf "Python 3.12.14\\n"; fi' > "$FAKE_BIN/python3"
  printf '%s\n' \
    '#!/bin/sh' \
    'printf "opencode 1.18.19\\n"' > "$OPENCODE"
  chmod +x "$FAKE_BIN"/*
}

write_env() {
  local control_url=${1:-http://127.0.0.1:8000}
  local workspace_id=${2:-20000000-0000-0000-0000-000000000001}
  local model=${3:-opencode-go/MiMo-V2.5}
  printf '%s\n' \
    '# fixture comment' \
    '' \
    'SIDERA_DATABASE_URL=postgresql+psycopg://sidera:fake-db-password@127.0.0.1:55432/sidera_r61_runtime' \
    'SIDERA_R6_1_TEST_DATABASE_URL=postgresql+psycopg://sidera:fake-db-password@127.0.0.1:55432/sidera_r61_cp_test' \
    "SIDERA_WORKSPACE_ID=$workspace_id" \
    "SIDERA_CONTROL_PLANE_URL=$control_url" \
    'SIDERA_MAIN_AGENT_GATEWAY_URL=http://127.0.0.1:8010' \
    'SIDERA_WEB_URL=http://127.0.0.1:4173' \
    'SIDERA_AKASHIC_ENDPOINT=/tmp/akashic/control.sock' \
    'SIDERA_AKASHIC_WORKSPACE_TOKEN=fake-akashic-workspace-token' \
    "SIDERA_AKASHIC_WORKSPACE=$AKASHIC_WORKSPACE" \
    "SIDERA_AKASHIC_CONFIG=$AKASHIC_CONFIG" \
    "SIDERA_OPENCODE_EXECUTABLE=$OPENCODE" \
    'SIDERA_OPENCODE_EXPECTED_VERSION=1.18.19' \
    "SIDERA_OPENCODE_MODEL=$model" \
    'SIDERA_PLANNING_PROFILE_ID=10000000-0000-0000-0000-000000000001' \
    'SIDERA_PLANNING_PROFILE_VERSION=1' > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

run_preflight() {
  PATH="$FAKE_BIN:/usr/bin:/bin" \
    SIDERA_R6_1_ENV_FILE="${1:-$ENV_FILE}" \
    SIDERA_R6_1_RUNTIME_DIR="$RUNTIME_DIR" \
    /bin/bash "$ROOT/scripts/r6-1-preflight.sh"
}

assert_failure() {
  local label=$1
  shift
  local output
  if output=$("$@" 2>&1); then
    printf '%s\n' "$output" >&2
    fail "$label should fail"
  fi
  case "$output" in
    *fake-db-password*|*fake-akashic-workspace-token*)
      fail "$label leaked a secret"
      ;;
  esac
}

make_fake_commands
write_env

if output=$(run_preflight 2>&1); then
  case "$output" in
    *fake-db-password*|*fake-akashic-workspace-token*) fail "valid preflight leaked a secret" ;;
  esac
else
  printf '%s\n' "$output" >&2
  fail "valid preflight should pass"
fi

awk '{printf "%s\r\n", $0}' "$ENV_FILE" > "$TMP/r6-1-crlf.env"
chmod 600 "$TMP/r6-1-crlf.env"
run_preflight "$TMP/r6-1-crlf.env" >/dev/null

printf '%s\n' 'SIDERA_WEB_URL=http://127.0.0.1:4174' >> "$ENV_FILE"
assert_failure "duplicate key" run_preflight
write_env

printf '%s\n' 'SIDERA_UNKNOWN=value' >> "$ENV_FILE"
assert_failure "unknown key" run_preflight
write_env

printf '%s\n' 'export SIDERA_WEB_URL=http://127.0.0.1:4174' >> "$ENV_FILE"
assert_failure "export syntax" run_preflight
write_env

backtick_marker=$(printf '\140')
printf 'SIDERA_WEB_URL=%stouch /tmp/nope%s\n' "$backtick_marker" "$backtick_marker" >> "$ENV_FILE"
assert_failure "backtick syntax" run_preflight
write_env

touch_target="$TMP/command-substitution-ran"
printf 'SIDERA_WEB_URL=$(touch %s)\n' "$touch_target" >> "$ENV_FILE"
assert_failure "command substitution" run_preflight
[ ! -e "$touch_target" ] || fail "command substitution was evaluated"
write_env

printf '%s\n' 'SIDERA_WEB_URL=http://127.0.0.1:4174;touch /tmp/nope' >> "$ENV_FILE"
assert_failure "semicolon syntax" run_preflight
write_env

printf '%s\n' 'SIDERA_WEB_URL=http://127.0.0.1:4174|touch /tmp/nope' >> "$ENV_FILE"
assert_failure "pipe syntax" run_preflight
write_env

sed 's/^SIDERA_AKASHIC_WORKSPACE_TOKEN=.*/SIDERA_AKASHIC_WORKSPACE_TOKEN=/' "$ENV_FILE" > "$TMP/missing-token.env"
chmod 600 "$TMP/missing-token.env"
assert_failure "missing token" run_preflight "$TMP/missing-token.env"

chmod 644 "$ENV_FILE"
assert_failure "world-readable env" run_preflight
chmod 600 "$ENV_FILE"

write_env http://192.0.2.10:8000
assert_failure "non-loopback control plane" run_preflight
write_env

write_env http://127.0.0.1:8000 invalid-workspace
assert_failure "invalid workspace UUID" run_preflight
write_env

sed 's#sidera_r61_cp_test#sidera_r61_runtime#' "$ENV_FILE" > "$TMP/same-db.env"
chmod 600 "$TMP/same-db.env"
assert_failure "duplicate database identity" run_preflight "$TMP/same-db.env"

sed 's/^SIDERA_OPENCODE_MODEL=.*/SIDERA_OPENCODE_MODEL=/' "$ENV_FILE" > "$TMP/missing-model.env"
chmod 600 "$TMP/missing-model.env"
assert_failure "missing model" run_preflight "$TMP/missing-model.env"

write_env http://127.0.0.1:8000 20000000-0000-0000-0000-000000000001 opencode-go/MiMo-V2.5
export R6_1_FAKE_PATCH_CAPABILITY=1
assert_failure "OpenCode patch capability" run_preflight
unset R6_1_FAKE_PATCH_CAPABILITY

export R6_1_FAKE_PORT_CONFLICT=1
assert_failure "occupied port" run_preflight
unset R6_1_FAKE_PORT_CONFLICT

export R6_1_FAKE_MIGRATION_FAIL=1
assert_failure "migration mismatch" run_preflight
unset R6_1_FAKE_MIGRATION_FAIL

sed 's/^SIDERA_OPENCODE_EXPECTED_VERSION=.*/SIDERA_OPENCODE_EXPECTED_VERSION=9.99.99/' "$ENV_FILE" > "$TMP/version-mismatch.env"
chmod 600 "$TMP/version-mismatch.env"
assert_failure "OpenCode version mismatch" run_preflight "$TMP/version-mismatch.env"

chmod 644 "$AKASHIC_CONFIG"
assert_failure "Akashic config permissions" run_preflight
chmod 600 "$AKASHIC_CONFIG"

printf '%s\n' 'r6-1 preflight tests passed'
