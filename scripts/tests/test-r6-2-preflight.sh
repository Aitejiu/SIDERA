#!/bin/bash
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r6-2-preflight.XXXXXX")
RUNTIME_DIR="$ROOT/.r6-2-preflight-runtime-$PPID"

FAKE_BIN="$TMP/bin"
ENV_FILE="$TMP/r6-2.local.env"
AKASHIC_NOT_ALLOWED="$TMP/should-not-be-read"
OPENCODE="$FAKE_BIN/opencode"
UV_LOG="$TMP/uv.log"
CURL_LOG="$TMP/curl.log"
CONTROL_PID=''
GATEWAY_PID=''
WEB_PID=''

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

cleanup() {
  for owned_pid in "$CONTROL_PID" "$GATEWAY_PID" "$WEB_PID"; do
    [ -z "$owned_pid" ] || kill "$owned_pid" >/dev/null 2>&1 || true
  done
  rm -rf "$TMP"
  rm -rf "$RUNTIME_DIR"
  rm -f "$ROOT/.r6-2-preflight-symlink-$PPID"
}

trap cleanup EXIT HUP INT TERM

make_fake_commands() {
  mkdir -p "$FAKE_BIN" "$RUNTIME_DIR" "$RUNTIME_DIR/pids" "$RUNTIME_DIR/identity"
  chmod 700 "$RUNTIME_DIR"

  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--prefix" ]; then printf "/opt/homebrew/opt/postgresql@16\\n"; exit 0; fi' \
    'exit 0' > "$FAKE_BIN/brew"

  printf '%s\n' \
    '#!/bin/sh' \
    'printf "%s|db=%s\\n" "$*" "${SIDERA_DATABASE_URL##*/}" >> "$SIDERA_R6_2_FAKE_UV_LOG"' \
    'case "$*" in' \
    '  *"python --version") printf "Python 3.12.14\\n" ;;' \
    '  *"opencode_preflight"*)' \
    '    if [ "$SIDERA_FAKE_OPENCODE_UNSUPPORTED" = "1" ]; then printf '\''%s\\n'\'' '\''{"status":"ok","handoff_status":"unsupported"}'\''; else printf '\''%s\\n'\'' '\''{"status":"ok","handoff_status":"supported"}'\''; fi' \
    '    ;;' \
    '  *"alembic"*"heads"*)' \
    '    if [ "${SIDERA_FAKE_MIGRATION_MULTI_HEAD:-0}" = "1" ]; then printf "0021_r6_2_retired_handoff_targets (head)\\n0021_other (head)\\n"; elif [ "${SIDERA_FAKE_MIGRATION_MISMATCH:-0}" = "1" ]; then printf "0019_r6_2_agent_handoff (head)\\n"; else printf "0021_r6_2_retired_handoff_targets (head)\\n"; fi' \
    '    ;;' \
    '  *"alembic"*"current"*)' \
    '    if [ "${SIDERA_FAKE_MIGRATION_MISMATCH:-0}" = "1" ]; then printf "0019_r6_2_agent_handoff (head)\\n"; else printf "0021_r6_2_retired_handoff_targets (head)\\n"; fi' \
    '    ;;' \
    '  *"alembic"*"history"*) printf "0021_r6_2_retired_handoff_targets -> 0020_r6_2_workspace_transfer_outcome -> 0019_r6_2_agent_handoff\\n" ;;' \
    '  *"alembic"*"upgrade head"*) printf "upgrade complete\\n" ;;' \
    '  *"test_database_schema.py"*) printf "3 passed\\n" ;;' \
    '  *"test_r6_2_real_opencode_handoff.py"*)' \
    '    [ "${SIDERA_FAKE_OPENCODE_REAL_PASS:-0}" = "1" ] && printf "1 passed\\n" || printf "1 skipped\\n"' \
    '    ;;' \
    '  *"test_r6_2_second_adapter_handoff.py"*)' \
    '    [ "${SIDERA_FAKE_SECOND_ADAPTER_PASS:-0}" = "1" ] && printf "1 passed\\n" || printf "1 skipped\\n"' \
    '    ;;' \
    'esac' > "$FAKE_BIN/uv"

  printf '%s\n' \
    '#!/bin/sh' \
    'printf "v24.19.0\\n"' > "$FAKE_BIN/node"
  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--version" ]; then printf "11.17.0\\n"; else printf "npm ok\\n"; fi' > "$FAKE_BIN/npm"
  printf '%s\n' \
    '#!/bin/sh' \
    'printf "%s\\n" "$*" >> "$SIDERA_R6_2_FAKE_CURL_LOG"' \
    'printf "{}\\n"' > "$FAKE_BIN/curl"
  printf '%s\n' '#!/bin/sh' 'exit 0' > "$FAKE_BIN/git"
  printf '%s\n' '#!/bin/sh' 'exit 0' > "$FAKE_BIN/pg_isready"
  printf '%s\n' '#!/bin/sh' 'printf "Python 3.12.14\\n"' > "$FAKE_BIN/python3"
  printf '%s\n' \
    '#!/bin/sh' \
    'case "$*" in' \
    '  *"-iTCP:8000"*) if [ "${SIDERA_FAKE_PORT_CONFLICT:-0}" = "1" ]; then printf "42424\\n"; else printf "%s\\n" "$SIDERA_FAKE_CONTROL_LISTENER_PID"; fi ;;' \
    '  *"-iTCP:8010"*) printf "%s\\n" "$SIDERA_FAKE_GATEWAY_LISTENER_PID" ;;' \
    '  *"-iTCP:4173"*) printf "%s\\n" "$SIDERA_FAKE_WEB_LISTENER_PID" ;;' \
    'esac' > "$FAKE_BIN/lsof"
  printf '%s\n' \
    '#!/bin/sh' \
    'pid=${2:-}' \
    'field=${4:-}' \
    'case "$field:$pid" in' \
    '  "ppid=:$SIDERA_FAKE_CONTROL_LISTENER_PID") printf "%s\\n" "$SIDERA_FAKE_CONTROL_PID" ;;' \
    '  "ppid=:$SIDERA_FAKE_GATEWAY_LISTENER_PID") printf "%s\\n" "$SIDERA_FAKE_GATEWAY_PID" ;;' \
    '  "ppid=:$SIDERA_FAKE_WEB_LISTENER_PID") printf "%s\\n" "$SIDERA_FAKE_WEB_PID" ;;' \
    '  "lstart=:$SIDERA_FAKE_CONTROL_PID"|"lstart=:$SIDERA_FAKE_GATEWAY_PID"|"lstart=:$SIDERA_FAKE_WEB_PID") printf "Mon Sep  2 12:00:00 2026\\n" ;;' \
    '  "args=:$SIDERA_FAKE_CONTROL_PID") printf "python -m uvicorn sidera_control_plane.api.app:app\\n" ;;' \
    '  "args=:$SIDERA_FAKE_GATEWAY_PID") printf "python -m uvicorn sidera_main_agent_gateway.api.app:app\\n" ;;' \
    '  "args=:$SIDERA_FAKE_WEB_PID") printf "npm run dev\\n" ;;' \
    'esac' > "$FAKE_BIN/ps"
  printf '%s\n' '#!/bin/sh' 'exit 0' > "$FAKE_BIN/shasum"

  printf '%s\n' \
    '#!/bin/sh' \
    'if [ "${1:-}" = "--version" ]; then printf "opencode 1.18.19\\n"; fi' > "$OPENCODE"
  chmod +x "$FAKE_BIN"/*
}

start_fake_runtime() {
  sleep 300 &
  CONTROL_PID=$!
  sleep 300 &
  GATEWAY_PID=$!
  sleep 300 &
  WEB_PID=$!

  export SIDERA_FAKE_CONTROL_PID=$CONTROL_PID
  export SIDERA_FAKE_GATEWAY_PID=$GATEWAY_PID
  export SIDERA_FAKE_WEB_PID=$WEB_PID
  export SIDERA_FAKE_CONTROL_LISTENER_PID=$((CONTROL_PID + 100000))
  export SIDERA_FAKE_GATEWAY_LISTENER_PID=$((GATEWAY_PID + 100000))
  export SIDERA_FAKE_WEB_LISTENER_PID=$((WEB_PID + 100000))

  printf '%s\n' "$CONTROL_PID" > "$RUNTIME_DIR/pids/control-plane.pid"
  printf '%s\n' "$GATEWAY_PID" > "$RUNTIME_DIR/pids/main-agent-gateway.pid"
  printf '%s\n' "$WEB_PID" > "$RUNTIME_DIR/pids/web.pid"

  write_identity control-plane "$CONTROL_PID" sidera_control_plane.api.app
  write_identity main-agent-gateway "$GATEWAY_PID" sidera_main_agent_gateway.api.app
  write_identity web "$WEB_PID" npm
}

write_identity() {
  local component=$1 pid=$2 command_label=$3 release=${4:-r5-3}
  printf 'pid=%s\ncomponent=%s\nrepo=%s\nmarker=sidera-%s:%s\nstarted_at=2026-09-02T12:00:00Z\nprocess_start=Mon Sep  2 12:00:00 2026\ncommand=%s\n' \
    "$pid" "$component" "$ROOT" "$release" "$component" "$command_label" \
    > "$RUNTIME_DIR/identity/$component.identity"
}

write_env() {
  local model=${1:-opencode-go/mimo-v2.5}
  local real_opencode=${2:-0}
  local second_adapter=${3:-0}
  local second_adapter_type=''
  [ "$second_adapter" != 1 ] || second_adapter_type=registered-second-adapter
  printf '%s\n' \
    '# fixture comment' \
    '' \
    'SIDERA_DATABASE_URL=postgresql+psycopg://sidera:fake-db-password@127.0.0.1:55432/sidera_r62_runtime' \
    'SIDERA_R6_2_TEST_DATABASE_URL=postgresql+psycopg://sidera:fake-db-password@127.0.0.1:55432/sidera_r62_test' \
    'SIDERA_WORKSPACE_ID=20000000-0000-0000-0000-000000000001' \
    'SIDERA_CONTROL_PLANE_URL=http://127.0.0.1:8000' \
    'SIDERA_MAIN_AGENT_GATEWAY_URL=http://127.0.0.1:8010' \
    'SIDERA_WEB_URL=http://127.0.0.1:4173' \
    "SIDERA_OPENCODE_EXECUTABLE=$OPENCODE" \
    'SIDERA_OPENCODE_EXPECTED_VERSION=1.18.19' \
    "SIDERA_OPENCODE_MODEL=$model" \
    "SIDERA_R6_2_REAL_OPENCODE=$real_opencode" \
    "SIDERA_R6_2_SECOND_ADAPTER=$second_adapter" \
    "SIDERA_R6_2_SECOND_ADAPTER_TYPE=$second_adapter_type" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

run_preflight() {
  local env_file=${1:-$ENV_FILE}
  shift || true
  PATH="$FAKE_BIN:/usr/bin:/bin" \
    SIDERA_R6_2_ENV_FILE="$env_file" \
    SIDERA_R6_2_RUNTIME_DIR="$RUNTIME_DIR" \
    SIDERA_R6_2_FAKE_UV_LOG="$UV_LOG" \
    SIDERA_R6_2_FAKE_CURL_LOG="$CURL_LOG" \
    /bin/bash "$ROOT/scripts/r6-2-preflight.sh" "$@"
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
    *fake-db-password*|*AKASHIC_NOT_ALLOWED*)
      fail "$label leaked a secret or disallowed provider value"
      ;;
  esac
}

make_fake_commands
start_fake_runtime
write_env

if output=$(run_preflight 2>&1); then
  case "$output" in
    *fake-db-password*|*SIDERA_DATABASE_URL=*) fail "valid preflight leaked private configuration" ;;
  esac
  case "$output" in
    *"deterministic=pass"*) ;; *) fail "valid preflight omitted deterministic result" ;; esac
  case "$output" in
    *"opencode_preflight=supported"*) ;; *) fail "valid preflight omitted OpenCode support" ;; esac
  case "$output" in
    *"opencode_real=absent"*) ;; *) fail "valid preflight misclassified absent OpenCode gate" ;; esac
  case "$output" in
    *"second_adapter_real=absent"*) ;; *) fail "valid preflight misclassified absent second Adapter gate" ;; esac
  case "$output" in
    *"release_classification=partial_gate"*) ;; *) fail "valid preflight omitted partial classification" ;; esac
else
  printf '%s\n' "$output" >&2
  fail "valid preflight should pass"
fi

case "$(cat "$CURL_LOG")" in
  *"http://127.0.0.1:8000/healthz"*) ;; *) fail "control-plane health route was not checked" ;;
esac
case "$(cat "$CURL_LOG")" in
  *"http://127.0.0.1:8010/main-agent/v1/health"*) ;; *) fail "main-agent health route was not checked" ;;
esac
case "$(cat "$CURL_LOG")" in
  *"http://127.0.0.1:4173/"*) ;; *) fail "Web root health route was not checked" ;;
esac

for release in r6-1 r6-2; do
  write_identity control-plane "$CONTROL_PID" sidera_control_plane.api.app "$release"
  write_identity main-agent-gateway "$GATEWAY_PID" sidera_main_agent_gateway.api.app "$release"
  write_identity web "$WEB_PID" npm "$release"
  run_preflight >/dev/null
done

write_identity control-plane "$CONTROL_PID" sidera_control_plane.api.app foreign
assert_failure "foreign identity marker" run_preflight
kill -0 "$CONTROL_PID" "$GATEWAY_PID" "$WEB_PID" >/dev/null 2>&1 \
  || fail "identity rejection terminated an owned service process"
write_identity control-plane "$CONTROL_PID" sidera_control_plane.api.app
write_identity main-agent-gateway "$GATEWAY_PID" sidera_main_agent_gateway.api.app
write_identity web "$WEB_PID" npm

case "$(cat "$UV_LOG")" in
  *"db=sidera_r62_test"*) ;; *) fail "migration checks did not use the disposable test database" ;;
esac
case "$(cat "$UV_LOG")" in
  *"db=sidera_r62_runtime"*) fail "migration test touched the development database" ;;
esac

help_output=$(PATH="$FAKE_BIN:/usr/bin:/bin" /bin/bash "$ROOT/scripts/r6-2-preflight.sh" --help)
case "$help_output" in *"usage:"*) ;; *) fail "--help omitted usage" ;; esac
assert_failure "unknown argument" env PATH="$FAKE_BIN:/usr/bin:/bin" /bin/bash "$ROOT/scripts/r6-2-preflight.sh" --unknown

awk '{printf "%s\r\n", $0}' "$ENV_FILE" > "$TMP/crlf.env"
chmod 600 "$TMP/crlf.env"
run_preflight "$TMP/crlf.env" >/dev/null

printf '%s\n' 'SIDERA_WEB_URL=http://127.0.0.1:4174' >> "$ENV_FILE"
assert_failure "duplicate key" run_preflight
write_env

printf '%s\n' 'SIDERA_UNKNOWN=value' >> "$ENV_FILE"
assert_failure "unknown key" run_preflight
write_env

printf '%s\n' 'export SIDERA_WEB_URL=http://127.0.0.1:4174' >> "$ENV_FILE"
assert_failure "export syntax" run_preflight
write_env

touch_target="$TMP/command-substitution-ran"
printf 'SIDERA_WEB_URL=$(touch %s)\n' "$touch_target" >> "$ENV_FILE"
assert_failure "command substitution" run_preflight
[ ! -e "$touch_target" ] || fail "command substitution was evaluated"
write_env

printf '%s\n' 'SIDERA_WEB_URL=http://not-a-url' >> "$ENV_FILE"
assert_failure "malformed loopback value" run_preflight
write_env

chmod 644 "$ENV_FILE"
assert_failure "world-readable env" run_preflight
chmod 600 "$ENV_FILE"

ln -s "$ENV_FILE" "$TMP/env-symlink"
assert_failure "symlink env" run_preflight "$TMP/env-symlink"

write_env
printf '%s\n' 'SIDERA_DATABASE_URL=postgresql://sidera:fake-db-password@192.0.2.10:55432/sidera_r62_runtime' >> "$TMP/non-loopback.env"
chmod 600 "$TMP/non-loopback.env"
assert_failure "non-loopback database" run_preflight "$TMP/non-loopback.env"

sed 's/sidera_r62_test/sidera_r62_runtime/' "$ENV_FILE" > "$TMP/same-db.env"
chmod 600 "$TMP/same-db.env"
assert_failure "duplicate database identity" run_preflight "$TMP/same-db.env"

write_env "invalid-model"
assert_failure "malformed model" run_preflight
write_env

sed 's/^SIDERA_R6_2_SECOND_ADAPTER=0$/SIDERA_R6_2_SECOND_ADAPTER=1/' \
  "$ENV_FILE" > "$TMP/missing-second-adapter-type.env"
chmod 600 "$TMP/missing-second-adapter-type.env"
assert_failure "missing second Adapter type" run_preflight "$TMP/missing-second-adapter-type.env"

export SIDERA_FAKE_PORT_CONFLICT=1
assert_failure "occupied port" run_preflight
unset SIDERA_FAKE_PORT_CONFLICT
kill -0 "$CONTROL_PID" "$GATEWAY_PID" "$WEB_PID" >/dev/null 2>&1 \
  || fail "foreign listener rejection terminated an owned service process"

export SIDERA_FAKE_MIGRATION_MISMATCH=1
assert_failure "migration mismatch" run_preflight
unset SIDERA_FAKE_MIGRATION_MISMATCH

export SIDERA_FAKE_MIGRATION_MULTI_HEAD=1
assert_failure "multiple migration heads" run_preflight
unset SIDERA_FAKE_MIGRATION_MULTI_HEAD

outside_runtime="$TMP/outside-runtime"
mkdir -p "$outside_runtime"
chmod 700 "$outside_runtime"
assert_failure "runtime outside repository" env PATH="$FAKE_BIN:/usr/bin:/bin" \
  SIDERA_R6_2_ENV_FILE="$ENV_FILE" SIDERA_R6_2_FAKE_UV_LOG="$UV_LOG" \
  /bin/bash "$ROOT/scripts/r6-2-preflight.sh" --runtime-dir "$outside_runtime"

symlink_runtime_target="$TMP/symlink-runtime-target"
mkdir -p "$symlink_runtime_target"
chmod 700 "$symlink_runtime_target"
symlink_runtime="$ROOT/.r6-2-preflight-symlink-$PPID"
ln -s "$symlink_runtime_target" "$symlink_runtime"
assert_failure "symlink runtime" env PATH="$FAKE_BIN:/usr/bin:/bin" \
  SIDERA_R6_2_ENV_FILE="$ENV_FILE" SIDERA_R6_2_FAKE_UV_LOG="$UV_LOG" \
  /bin/bash "$ROOT/scripts/r6-2-preflight.sh" --runtime-dir "$symlink_runtime"
rm -f "$symlink_runtime"

chmod 755 "$RUNTIME_DIR"
assert_failure "runtime permissions" run_preflight
chmod 700 "$RUNTIME_DIR"

export SIDERA_FAKE_OPENCODE_UNSUPPORTED=1
if output=$(run_preflight 2>&1); then
  case "$output" in
    *"opencode_preflight=unsupported"*) ;; *) fail "unsupported OpenCode status was not reported" ;; esac
  case "$output" in
    *"release_classification=partial_gate"*) ;; *) fail "unsupported OpenCode status changed classification incorrectly" ;; esac
else
  printf '%s\n' "$output" >&2
  fail "unsupported OpenCode preflight should remain a non-blocking partial gate"
fi
unset SIDERA_FAKE_OPENCODE_UNSUPPORTED

write_env opencode-go/mimo-v2.5 1 1
export SIDERA_FAKE_OPENCODE_REAL_PASS=1
export SIDERA_FAKE_SECOND_ADAPTER_PASS=1
if output=$(run_preflight 2>&1); then
  case "$output" in *"opencode_real=pass"*) ;; *) fail "real OpenCode pass was not classified" ;; esac
  case "$output" in *"second_adapter_real=pass"*) ;; *) fail "second Adapter pass was not classified" ;; esac
  case "$output" in *"release_classification=complete"*) ;; *) fail "complete classification was not emitted" ;; esac
else
  printf '%s\n' "$output" >&2
  fail "fake successful real-gate discovery should pass preflight"
fi

printf '%s\n' 'r6-2 preflight tests passed'
