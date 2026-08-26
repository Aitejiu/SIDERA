#!/bin/bash
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r5-3-preflight.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

FAKE_BIN="$TMP/bin"
ENV_FILE="$TMP/r5-3.local.env"
RUNTIME_DIR="$TMP/runtime"
SPOOL_DIR="$RUNTIME_DIR/candidate-spool"
AKASHIC_WORKSPACE="$TMP/akashic-workspace"
AKASHIC_CONFIG="$TMP/akashic-config.toml"
OPENCODE="$FAKE_BIN/opencode"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

make_fake_commands() {
  mkdir -p "$FAKE_BIN" "$RUNTIME_DIR/pids" "$RUNTIME_DIR/logs" "$RUNTIME_DIR/identity"
  mkdir -p "$SPOOL_DIR" "$AKASHIC_WORKSPACE"
  chmod 700 "$SPOOL_DIR" "$AKASHIC_WORKSPACE"
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
    '  *alembic*)' \
    '    [ "${R5_3_FAKE_MIGRATION_FAIL:-0}" = "1" ] && exit 42' \
    '    case "$*" in' \
    '      *services/knowledge-service*) printf "0001_knowledge_foundation (head)\\n" ;;' \
    '      *) printf "0016_r5_3_work_context_packages (head)\\n" ;;' \
    '    esac' \
    '    exit 0 ;;' \
    'esac' \
    'exit 0' > "$FAKE_BIN/uv"

  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--version" ]; then printf "v24.19.0\\n"; exit 0; fi' \
    'printf "v24.19.0\\n"' > "$FAKE_BIN/node"
  printf '%s\n' \
    '#!/bin/sh' \
    'printf "11.17.0\\n"' > "$FAKE_BIN/npm"
  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--version" ]; then printf "curl 8.0.0\\n"; exit 0; fi' \
    'printf "{}\\n"' > "$FAKE_BIN/curl"
  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--version" ]; then printf "git version 2.40.0\\n"; exit 0; fi' \
    'case "$*" in' \
    '  *"rev-parse HEAD:akashic-agent"*) printf "akashic-test-tree\\n" ;;' \
    '  *"rev-parse --is-inside-work-tree"*) printf "true\\n" ;;' \
    'esac' \
    'exit 0' > "$FAKE_BIN/git"
  printf '%s\n' \
    '#!/bin/sh' \
    '[ "${R5_3_FAKE_PORT_CONFLICT:-0}" = "1" ] && printf "99999\\n"' > "$FAKE_BIN/lsof"
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
  local profile_id=${3:-10000000-0000-0000-0000-000000000001}
  printf '%s\n' \
    '# fixture comment' \
    '' \
    'SIDERA_DATABASE_URL=postgresql+psycopg://sidera:fake-db-password@127.0.0.1:55432/sidera_r53_runtime' \
    'SIDERA_R5_3_TEST_DATABASE_URL=postgresql+psycopg://sidera:fake-db-password@127.0.0.1:55432/sidera_r53_cp_test' \
    'SIDERA_KNOWLEDGE_DATABASE_URL=postgresql+psycopg://sidera:fake-db-password@127.0.0.1:55432/sidera_r53_knowledge_runtime' \
    'SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL=postgresql+psycopg://sidera:fake-db-password@127.0.0.1:55432/sidera_r53_knowledge_test' \
    "SIDERA_WORKSPACE_ID=$workspace_id" \
    "SIDERA_CONTROL_PLANE_URL=$control_url" \
    'SIDERA_KNOWLEDGE_BASE_URL=http://127.0.0.1:8020' \
    'SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN=fake-knowledge-local-owner-token' \
    'SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN=fake-knowledge-control-plane-token' \
    'SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN=fake-knowledge-main-agent-token' \
    'SIDERA_MAIN_AGENT_GATEWAY_URL=http://127.0.0.1:8010' \
    'SIDERA_WEB_URL=http://127.0.0.1:4173' \
    'SIDERA_CONTEXT_PACKAGE_CUTOVER_AT=2026-08-27T00:00:00+08:00' \
    "SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR=$SPOOL_DIR" \
    'SIDERA_AKASHIC_ENDPOINT=/tmp/akashic/control.sock' \
    'SIDERA_AKASHIC_WORKSPACE_TOKEN=fake-akashic-workspace-token' \
    "SIDERA_AKASHIC_WORKSPACE=$AKASHIC_WORKSPACE" \
    "SIDERA_AKASHIC_CONFIG=$AKASHIC_CONFIG" \
    "SIDERA_OPENCODE_EXECUTABLE=$OPENCODE" \
    'SIDERA_OPENCODE_EXPECTED_VERSION=1.18.19' \
    'SIDERA_OPENCODE_MODEL=opencode-go/MiMo-V2.5' \
    'SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS=300' \
    "SIDERA_PLANNING_PROFILE_ID=$profile_id" \
    'SIDERA_PLANNING_PROFILE_VERSION=1' > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

run_preflight() {
  PATH="$FAKE_BIN:/usr/bin:/bin" \
    SIDERA_R5_3_ENV_FILE="${1:-$ENV_FILE}" \
    SIDERA_R5_3_RUNTIME_DIR="$RUNTIME_DIR" \
    /bin/bash "$ROOT/scripts/r5-3-preflight.sh"
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
    *fake-db-password*|*fake-knowledge-local-owner-token*|*fake-knowledge-control-plane-token*|*fake-knowledge-main-agent-token*|*fake-akashic-workspace-token*)
      fail "$label leaked a secret"
      ;;
  esac
}

make_fake_commands
write_env

if output=$(run_preflight 2>&1); then
  case "$output" in
    *fake-db-password*|*fake-knowledge-local-owner-token*|*fake-knowledge-control-plane-token*|*fake-knowledge-main-agent-token*|*fake-akashic-workspace-token*)
      fail "valid preflight leaked a secret"
      ;;
  esac
else
  printf '%s\n' "$output" >&2
  fail "valid preflight should pass"
fi

awk '{printf "%s\r\n", $0}' "$ENV_FILE" > "$TMP/r5-3-crlf.env"
chmod 600 "$TMP/r5-3-crlf.env"
if output=$(run_preflight "$TMP/r5-3-crlf.env" 2>&1); then
  :
else
  printf '%s\n' "$output" >&2
  fail "CRLF env should pass"
fi

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

sed 's/^SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN=.*/SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN=/' "$ENV_FILE" > "$TMP/missing-token.env"
chmod 600 "$TMP/missing-token.env"
assert_failure "empty knowledge token" run_preflight "$TMP/missing-token.env"

chmod 644 "$ENV_FILE"
assert_failure "world-readable env" run_preflight
chmod 600 "$ENV_FILE"

write_env http://192.0.2.10:8000
assert_failure "non-loopback control plane" run_preflight
write_env

write_env http://127.0.0.1:8000 invalid-workspace
assert_failure "invalid workspace UUID" run_preflight
write_env

sed 's/+08:00//' "$ENV_FILE" > "$TMP/no-timezone.env"
chmod 600 "$TMP/no-timezone.env"
assert_failure "timezone-less cutover" run_preflight "$TMP/no-timezone.env"

sed 's/sidera_r53_cp_test/sidera_r53_runtime/' "$ENV_FILE" > "$TMP/same-db.env"
chmod 600 "$TMP/same-db.env"
assert_failure "duplicate database identity" run_preflight "$TMP/same-db.env"

mkdir -p "$TMP/outside-spool"
chmod 700 "$TMP/outside-spool"
sed "s|$SPOOL_DIR|$TMP/outside-spool|" "$ENV_FILE" > "$TMP/outside-spool.env"
chmod 600 "$TMP/outside-spool.env"
assert_failure "spool outside runtime" run_preflight "$TMP/outside-spool.env"

mv "$SPOOL_DIR" "$TMP/spool-real"
ln -s "$TMP/spool-real" "$SPOOL_DIR"
assert_failure "symlink spool" run_preflight
rm "$SPOOL_DIR"
mv "$TMP/spool-real" "$SPOOL_DIR"

export R5_3_FAKE_PORT_CONFLICT=1
assert_failure "occupied port" run_preflight
unset R5_3_FAKE_PORT_CONFLICT

export R5_3_FAKE_MIGRATION_FAIL=1
assert_failure "migration mismatch" run_preflight
unset R5_3_FAKE_MIGRATION_FAIL

mv "$FAKE_BIN/uv" "$FAKE_BIN/uv.disabled"
assert_failure "missing uv" run_preflight
mv "$FAKE_BIN/uv.disabled" "$FAKE_BIN/uv"

printf '%s\n' 'r5-3 preflight tests passed'
