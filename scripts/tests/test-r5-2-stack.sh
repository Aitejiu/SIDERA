#!/bin/bash
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r5-2-test.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

FAKE_BIN="$TMP/bin"
ENV_FILE="$TMP/r5-2.local.env"
RUNTIME_DIR="$TMP/runtime"
mkdir -p "$FAKE_BIN" "$TMP/akashic"
touch "$TMP/akashic/config.toml"

make_fake_commands() {
  printf '%s\n' '#!/bin/sh' 'case "$1" in' '  --version) printf "uv 0.6.0\\n" ;;' '  *) case "$*" in *alembic*heads*) printf "0015_r5_1_1_worker_runtime_binding (head)\\n" ;; esac ;;' 'esac' > "$FAKE_BIN/uv"
  printf '%s\n' '#!/bin/sh' 'printf "11.17.0\\n"' > "$FAKE_BIN/npm"
  printf '%s\n' '#!/bin/sh' 'printf "curl 8.0.0\\n"' > "$FAKE_BIN/curl"
  printf '%s\n' '#!/bin/sh' 'if [ "${1:-}" = "--version" ]; then printf "git version 2.40.0\\n"; fi' > "$FAKE_BIN/git"
  printf '%s\n' '#!/bin/sh' 'exit 0' > "$FAKE_BIN/pg_isready"
  printf '%s\n' '#!/bin/sh' 'if [ "${1:-}" = "--version" ]; then printf "Python 3.12.14\\n"; fi' > "$FAKE_BIN/python3"
  printf '%s\n' '#!/bin/sh' 'printf "v24.19.0\\n"' > "$FAKE_BIN/node"
  printf '%s\n' '#!/bin/sh' 'exit 0' > "$FAKE_BIN/lsof"
  chmod +x "$FAKE_BIN"/*
}

write_env() {
  local control_url=${1:-http://127.0.0.1:8000}
  local workspace_id=${2:-20000000-0000-0000-0000-000000000001}
  local profile_id=${3:-10000000-0000-0000-0000-000000000001}
  cat > "$ENV_FILE" <<EOF
SIDERA_DATABASE_URL=postgresql+psycopg://sidera:fake-db-password@127.0.0.1:55432/sidera_test
SIDERA_WORKSPACE_ID=$workspace_id
SIDERA_PLANNING_PROFILE_ID=$profile_id
SIDERA_PLANNING_PROFILE_VERSION=1
SIDERA_CONTROL_PLANE_URL=$control_url
SIDERA_WEB_URL=http://127.0.0.1:4174
SIDERA_MAIN_AGENT_GATEWAY_URL=http://127.0.0.1:8010
SIDERA_AKASHIC_ENDPOINT=/tmp/akashic/control.sock
SIDERA_AKASHIC_WORKSPACE_TOKEN=fake-akashic-token
SIDERA_AKASHIC_WORKSPACE=$TMP/akashic
SIDERA_AKASHIC_CONFIG=$TMP/akashic/config.toml
SIDERA_OPENCODE_EXECUTABLE=/usr/bin/true
SIDERA_OPENCODE_EXPECTED_VERSION=1.18.19
SIDERA_OPENCODE_MODEL=deepseek/deepseek-v4-flash
EOF
  chmod 600 "$ENV_FILE"
}

run_preflight() {
  PATH="$FAKE_BIN:/usr/bin:/bin" \
    SIDERA_R5_2_ENV_FILE="$ENV_FILE" \
    SIDERA_R5_2_RUNTIME_DIR="$RUNTIME_DIR" \
    /bin/bash "$ROOT/scripts/r5-2-preflight.sh"
}

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

make_fake_commands
write_env

if ! output=$(run_preflight 2>&1); then
  printf '%s\n' "$output" >&2
  fail "valid preflight should pass"
fi
case "$output" in
  *fake-db-password*|*fake-akashic-token*) fail "preflight leaked a secret" ;;
esac
if [ -e "$RUNTIME_DIR" ]; then
  fail "preflight created runtime state"
fi

mv "$FAKE_BIN/uv" "$FAKE_BIN/uv.disabled"
if run_preflight >/dev/null 2>&1; then
  fail "missing uv should fail"
fi
mv "$FAKE_BIN/uv.disabled" "$FAKE_BIN/uv"

printf '%s\n' '#!/bin/sh' 'printf "Python 3.11.9\\n"' > "$FAKE_BIN/python3"
if run_preflight >/dev/null 2>&1; then
  fail "unsupported Python should fail"
fi
make_fake_commands

write_env "http://192.0.2.10:8000"
if run_preflight >/dev/null 2>&1; then
  fail "non-loopback Control Plane URL should fail"
fi

write_env http://127.0.0.1:8000 invalid-workspace 10000000-0000-0000-0000-000000000001
if run_preflight >/dev/null 2>&1; then
  fail "invalid workspace UUID should fail"
fi

write_env
printf '%s\n' 'SIDERA_WEB_URL=$(touch /tmp/should-not-run)' >> "$ENV_FILE"
if run_preflight >/dev/null 2>&1; then
  fail "command substitution in env should fail"
fi
if [ -e /tmp/should-not-run ]; then
  fail "env parser evaluated command substitution"
fi

printf 'r5-2 preflight tests passed\n'
