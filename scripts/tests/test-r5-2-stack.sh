#!/bin/bash
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r5-2-test.XXXXXX")
cleanup() {
  if [ -f "${ENV_FILE:-}" ]; then
    PATH="${FAKE_BIN:-/usr/bin:/bin}" \
      SIDERA_R5_2_ENV_FILE="$ENV_FILE" \
      SIDERA_R5_2_RUNTIME_DIR="${RUNTIME_DIR:-$TMP/runtime}" \
      /bin/bash "$ROOT/scripts/r5-2-stack.sh" stop >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT HUP INT TERM

FAKE_BIN="$TMP/bin"
ENV_FILE="$TMP/r5-2.local.env"
RUNTIME_DIR="$TMP/runtime"
mkdir -p "$FAKE_BIN" "$TMP/akashic"
touch "$TMP/akashic/config.toml"

make_fake_commands() {
  printf '%s\n' '#!/bin/sh' 'if [ -n "${R5_2_GATE_LOG:-}" ]; then printf "uv %s\\n" "$*" >> "$R5_2_GATE_LOG"; fi' 'if [ -n "${R5_2_UV_LOG:-}" ]; then printf "uv %s:adapter_kind=%s\\n" "$*" "${SIDERA_MAIN_AGENT_ADAPTER_KIND:-<unset>}" >> "$R5_2_UV_LOG"; fi' 'if [ "${R5_2_GATE_FAIL:-0}" = "1" ]; then' '  case "$*" in *services/control-plane/tests/unit*) exit 42 ;; esac' 'fi' 'case "${1:-}" in' '  --version) printf "uv 0.6.0\\n"; exit 0 ;;' 'esac' 'case "$*" in' '  *alembic*heads*) printf "0015_r5_1_1_worker_runtime_binding (head)\\n" ;;' '  *uvicorn*|*sidera_control_plane.planning_worker*) while :; do sleep 1; done ;;' 'esac' > "$FAKE_BIN/uv"
  printf '%s\n' '#!/bin/sh' 'if [ -n "${R5_2_GATE_LOG:-}" ]; then printf "npm %s\\n" "$*" >> "$R5_2_GATE_LOG"; fi' 'if [ "${1:-}" = "--version" ]; then printf "11.17.0\\n"; exit 0; fi' 'case "$*" in *run*dev*) while :; do sleep 1; done ;; esac' > "$FAKE_BIN/npm"
  cat > "$FAKE_BIN/curl" <<'EOF'
#!/bin/sh
if [ "${R5_2_FAKE_CURL_FAIL:-0}" = "1" ]; then exit 1; fi
case "$*" in
  *main-agent*) printf '%s\n' '{"provider":"available"}' ;;
  *) printf '%s\n' '{}' ;;
esac
EOF
  printf '%s\n' '#!/bin/sh' 'if [ "${1:-}" = "--version" ]; then printf "git version 2.40.0\\n"; fi' > "$FAKE_BIN/git"
  printf '%s\n' '#!/bin/sh' 'exit 0' > "$FAKE_BIN/pg_isready"
  printf '%s\n' '#!/bin/sh' 'if [ "${1:-}" = "--version" ]; then printf "Python 3.12.14\\n"; exit 0; fi' 'if [ "${1:-}" = "-c" ]; then shift 2; exec "$@"; fi' 'exit 0' > "$FAKE_BIN/python3"
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
SIDERA_R5_2_TEST_DATABASE_URL=postgresql+psycopg://sidera:fake-db-password@127.0.0.1:55432/sidera_r5_2_gate
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
SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS=300
EOF
  chmod 600 "$ENV_FILE"
}

run_preflight() {
  local env_file=${1:-$ENV_FILE}
  PATH="$FAKE_BIN:/usr/bin:/bin" \
    SIDERA_R5_2_ENV_FILE="$env_file" \
    SIDERA_R5_2_RUNTIME_DIR="$RUNTIME_DIR" \
    /bin/bash "$ROOT/scripts/r5-2-preflight.sh"
}

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

make_fake_commands
export R5_2_UV_LOG="$TMP/uv.log"
write_env

if gate_output=$(
  PATH="$FAKE_BIN:/usr/bin:/bin" \
    SIDERA_R5_2_ENV_FILE="$ENV_FILE" \
    SIDERA_R5_2_RUNTIME_DIR="$RUNTIME_DIR" \
    /bin/bash "$ROOT/scripts/r5-2-release-gate.sh" 2>&1
); then
  fail "release gate should require the real Main Agent flag"
fi
case "$gate_output" in
  *'SIDERA_RUN_REAL_MAIN_AGENT=1'*) ;;
  *) printf '%s\n' "$gate_output" >&2; fail "release gate flag error was unclear" ;;
esac

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

write_env
sed '/^SIDERA_AKASHIC_WORKSPACE_TOKEN=/d' "$ENV_FILE" > "$TMP/missing-token.env"
chmod 600 "$TMP/missing-token.env"
if run_preflight "$TMP/missing-token.env" >/dev/null 2>&1; then
  fail "missing Akashic workspace token should fail"
fi

printf 'r5-2 preflight tests passed\n'

write_env
export PATH="$FAKE_BIN:/usr/bin:/bin"
export SIDERA_R5_2_ENV_FILE="$ENV_FILE"
export SIDERA_R5_2_RUNTIME_DIR="$RUNTIME_DIR"

if ! output=$(/bin/bash "$ROOT/scripts/r5-2-stack.sh" start 2>&1); then
  printf '%s\n' "$output" >&2
  fail "stack start should pass with fake services"
fi
case "$output" in
  *fake-db-password*|*fake-akashic-token*) fail "stack output leaked a secret" ;;
esac

components="control-plane planning-worker main-agent-gateway web"
for component in $components; do
  [ -f "$RUNTIME_DIR/pids/$component.pid" ] || fail "missing PID file for $component"
  [ -f "$RUNTIME_DIR/identity/$component.identity" ] || fail "missing identity for $component"
done
gateway_argv=$(grep -F "services/main-agent-gateway" "$R5_2_UV_LOG" | tail -n 1)
case "$gateway_argv" in
  *'--extra akashic --extra demo uvicorn sidera_main_agent_gateway.api.app:create_app --factory --host 127.0.0.1 --port 8010:adapter_kind=<unset>'*) ;;
  *) fail "gateway start must select Akashic and Demo extras without forcing an adapter kind" ;;
esac
if grep -F 'services/control-plane' "$R5_2_UV_LOG" | grep -F -- '--extra' >/dev/null 2>&1; then
  fail "control-plane commands must not receive Gateway extras"
fi

if ! gate_output=$(
  SIDERA_RUN_REAL_MAIN_AGENT=1 \
    SIDERA_REAL_MAIN_AGENT_PROVIDER=fake-provider \
    SIDERA_REAL_GATE_HELPER=/usr/bin/true \
    R5_2_GATE_LOG="$TMP/gate.log" \
    /bin/bash "$ROOT/scripts/r5-2-release-gate.sh" 2>&1
); then
  printf '%s\n' "$gate_output" >&2
  fail "release gate should pass with fake commands"
fi
case "$gate_output" in
  *'r5-2 release gate: passed'*) ;;
  *) fail "release gate did not report success" ;;
esac

if gate_output=$(
  SIDERA_RUN_REAL_MAIN_AGENT=1 \
    SIDERA_REAL_MAIN_AGENT_PROVIDER=fake-provider \
    SIDERA_REAL_GATE_HELPER=/usr/bin/true \
    R5_2_GATE_FAIL=1 \
    R5_2_GATE_LOG="$TMP/gate-fail.log" \
    /bin/bash "$ROOT/scripts/r5-2-release-gate.sh" 2>&1
); then
  fail "release gate should stop after a failed step"
fi
case "$gate_output" in *'control-plane unit'*) ;; *) fail "release gate did not expose failed step" ;; esac
case "$gate_output" in *'Akashic plugin tests'*) fail "release gate continued after failure" ;; esac

first_pids=''
for component in $components; do
  first_pids="$first_pids $(sed -n '1p' "$RUNTIME_DIR/pids/$component.pid")"
done
if ! /bin/bash "$ROOT/scripts/r5-2-stack.sh" start >/dev/null 2>&1; then
  fail "second stack start should be idempotent"
fi
second_pids=''
for component in $components; do
  second_pids="$second_pids $(sed -n '1p' "$RUNTIME_DIR/pids/$component.pid")"
done
[ "$first_pids" = "$second_pids" ] || fail "second start created duplicate processes"

/bin/bash "$ROOT/scripts/r5-2-stack.sh" status >/dev/null 2>&1 || fail "healthy stack status should pass"
for component in $components; do
  /bin/bash "$ROOT/scripts/r5-2-stack.sh" logs "$component" >/dev/null || fail "logs should resolve $component"
done

if /bin/bash "$ROOT/scripts/r5-2-stack.sh" invalid-action >/dev/null 2>&1; then
  fail "unknown action should fail"
fi
if /bin/bash "$ROOT/scripts/r5-2-stack.sh" logs unknown >/dev/null 2>&1; then
  fail "unknown component should fail"
fi

/bin/bash "$ROOT/scripts/r5-2-stack.sh" stop >/dev/null 2>&1 || fail "stack stop should pass"
status_output=$(/bin/bash "$ROOT/scripts/r5-2-stack.sh" status 2>&1) || fail "stopped stack status should be readable"
case "$status_output" in *'control-plane: stopped'*) ;; *) fail "stopped stack status was not reported" ;; esac

# An unrelated process in a PID file is quarantined and never signalled.
sleep 30 &
unrelated_pid=$!
mkdir -p "$RUNTIME_DIR/pids" "$RUNTIME_DIR/identity"
printf '%s\n' "$unrelated_pid" > "$RUNTIME_DIR/pids/control-plane.pid"
printf 'pid=%s\ncomponent=control-plane\nrepo=%s\nmarker=sidera-r5-2:control-plane\n' "$unrelated_pid" "$ROOT" > "$RUNTIME_DIR/identity/control-plane.identity"
if ! /bin/bash "$ROOT/scripts/r5-2-stack.sh" start >/dev/null 2>&1; then
  kill "$unrelated_pid" >/dev/null 2>&1 || true
  fail "stack should quarantine a stale PID and start"
fi
ls "$RUNTIME_DIR/pids/control-plane.pid.stale."* >/dev/null 2>&1 || fail "stale PID was not quarantined"
kill -0 "$unrelated_pid" >/dev/null 2>&1 || fail "stack signalled an unrelated PID"
kill "$unrelated_pid" >/dev/null 2>&1 || true
/bin/bash "$ROOT/scripts/r5-2-stack.sh" stop >/dev/null 2>&1 || true

# A health failure rolls back only this invocation's newly started processes.
export SIDERA_R5_2_HEALTH_ATTEMPTS=2
export R5_2_FAKE_CURL_FAIL=1
if /bin/bash "$ROOT/scripts/r5-2-stack.sh" start >/dev/null 2>&1; then
  fail "health failure should make start non-zero"
fi
unset R5_2_FAKE_CURL_FAIL SIDERA_R5_2_HEALTH_ATTEMPTS
for component in $components; do
  [ ! -f "$RUNTIME_DIR/pids/$component.pid" ] || fail "failed start left $component PID"
done

printf 'r5-2 lifecycle tests passed\n'
