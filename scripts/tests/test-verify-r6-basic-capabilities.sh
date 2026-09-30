#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
GATE="$ROOT/scripts/verify-r6-basic-capabilities.sh"
TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r6-basic-capabilities-test.XXXXXX")
FAKE_BIN="$TEMP_DIR/bin"
FAKE_LOG="$TEMP_DIR/fake.log"
N1_ROOT="$TEMP_DIR/n1-audit"
mkdir -p "$FAKE_BIN"
mkdir -p \
  "$N1_ROOT/runtime" \
  "$N1_ROOT/source-repository" \
  "$N1_ROOT/lm-4.3-backup" \
  "$N1_ROOT/upgrade-target" \
  "$N1_ROOT/rollback-target"
printf '%s\n' '{}' >"$N1_ROOT/runtime/source-manifest.json"
printf '%s\n' '{}' >"$N1_ROOT/runtime/source-inventory.json"
printf '%s\n' '{}' >"$N1_ROOT/lm-4.3-backup/backup-manifest.json"
printf '%s\n' '{}' >"$N1_ROOT/upgrade-target/restore-result.json"
printf '%s\n' '{}' >"$N1_ROOT/rollback-target/restore-result.json"
trap 'rm -rf "$TEMP_DIR"' EXIT

fail() {
  printf 'basic capabilities gate test failed: %s\n' "$1" >&2
  exit 1
}

assert_contains() {
  local haystack=$1
  local needle=$2
  [[ "$haystack" == *"$needle"* ]] || fail "expected output to contain: $needle"
}

assert_not_contains() {
  local haystack=$1
  local needle=$2
  [[ "$haystack" != *"$needle"* ]] || fail "expected output not to contain: $needle"
}

REAL_GIT=$(command -v git)
cat >"$FAKE_BIN/git" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'git deepseek=%s opencode=%s args=%s\n' \
  "${DEEPSEEK_API_KEY:-unset}" "${OPENCODE_API_KEY:-unset}" "$*" >>"$FAKE_LOG"
if [[ "$*" == *"status --short -- .superpowers references"* ]]; then
  if [[ "${FAKE_GIT_SCOPE:-protected}" == "dirty" ]]; then
    printf '%s\n' ' M services/control-plane/src/sidera_control_plane/app.py'
  else
    printf '%s\n' '?? .superpowers/' '?? references/'
  fi
  exit 0
fi
if [[ "$*" == *"diff --check"* ]]; then
  exit 0
fi
exec "$REAL_GIT" "$@"
SH

cat >"$FAKE_BIN/uv" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'uv deepseek=%s opencode=%s args=%s\n' \
  "${DEEPSEEK_API_KEY:-unset}" "${OPENCODE_API_KEY:-unset}" "$*" >>"$FAKE_LOG"
if [[ "$*" == *"local_gate_record classify"* ]]; then
  printf '%s\n' "${FAKE_GIT_SCOPE:-protected-only}"
  exit 0
fi
if [[ "$*" == *"local_gate_record fingerprint"* ]]; then
  printf '%s\n' "${FAKE_FINGERPRINT:-current-fingerprint}"
  exit 0
fi
if [[ "$*" == *"r6-effective-config.py"* ]]; then
  if [[ "${FAKE_REAL_CONFIG:-unavailable}" == "pass" ]]; then
    printf '%s\n' '{"status":"verified"}'
    exit 0
  fi
  printf '%s\n' '{"status":"unavailable","error_code":"model_config_missing"}'
  exit 2
fi
if [[ "$*" == *"r6-basic-acceptance.py"* ]]; then
  if [[ "${FAKE_REAL_ACCEPTANCE_MODE:-valid}" == "malformed" ]]; then
    printf '%s\n' 'acceptance transport exploded' >&2
    exit 1
  fi
  printf '%s\n' '{"status":"INCOMPLETE","failure_code":"fake_observation_missing"}'
  exit 2
fi
if [[ "$*" == *"local_gate_record write"* ]]; then
  for ((index = 1; index <= $#; index += 1)); do
    if [[ "${!index}" == "--output" ]]; then
      next=$((index + 1))
      printf '%s\n' '{"status":"PASS"}' >"${!next}"
    fi
  done
  exit 0
fi
if [[ "$*" == *"openapi_contract"* ]]; then
  for ((index = 1; index <= $#; index += 1)); do
    if [[ "${!index}" == "--output" ]]; then
      next=$((index + 1))
      cp "$FAKE_REPO_ROOT/apps/web/openapi.json" "${!next}"
    fi
  done
fi
if [[ "${FAKE_PYTEST_MODE:-pass}" == "zero" && "$*" == *pytest* ]]; then
  printf '%s\n' 'no tests ran'
  exit 0
fi
if [[ "${FAKE_PYTEST_MODE:-pass}" == "skip" && "$*" == *pytest* ]]; then
  printf '%s\n' '2 skipped'
  exit 0
fi
printf '%s\n' '1 passed'
SH

cat >"$FAKE_BIN/npm" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'npm deepseek=%s opencode=%s args=%s\n' \
  "${DEEPSEEK_API_KEY:-unset}" "${OPENCODE_API_KEY:-unset}" "$* suite=${SIDERA_BASIC_FAKE_SUITE:-unset}" >>"$FAKE_LOG"
for ((index = 1; index <= $#; index += 1)); do
  if [[ "${!index}" == "-o" ]]; then
    next=$((index + 1))
    cp "$FAKE_REPO_ROOT/apps/web/src/shared/api/schema.d.ts" "${!next}"
  fi
done
if [[ "$*" == *"playwright.basic-maintenance.config.ts"* ]]; then
  if [[ "${FAKE_BROWSER_MODE:-available}" == "missing" ]]; then
    printf '%s\n' 'browser executable missing' >&2
    exit 127
  fi
  printf '%s\n' '3 passed'
  exit 0
fi
printf '%s\n' '1 passed'
SH

chmod +x "$FAKE_BIN/git" "$FAKE_BIN/uv" "$FAKE_BIN/npm"

bash "$ROOT/scripts/tests/test-r6-basic-stack.sh" >/dev/null

run_gate() {
  PATH="$FAKE_BIN:$PATH" \
    REAL_GIT="$REAL_GIT" \
    FAKE_LOG="$FAKE_LOG" \
    FAKE_REPO_ROOT="$ROOT" \
    SIDERA_BASIC_CAPABILITY_TEST_MODE=1 \
    SIDERA_TEST_DATABASE_URL="${SIDERA_TEST_DATABASE_URL-postgresql+psycopg://tester@127.0.0.1:55432/sidera_test}" \
    SIDERA_KNOWLEDGE_TEST_DATABASE_URL="${SIDERA_KNOWLEDGE_TEST_DATABASE_URL-postgresql+psycopg://tester@127.0.0.1:55432/sidera_knowledge_test}" \
    SIDERA_N1_AUDIT_ROOT="${SIDERA_N1_AUDIT_ROOT-$N1_ROOT}" \
    SIDERA_RUN_WEB_E2E="${SIDERA_RUN_WEB_E2E-1}" \
    bash "$GATE" "$@"
}

set +e
default_output=$(DEEPSEEK_API_KEY=sk-test-deepseek OPENCODE_API_KEY=sk-test-opencode run_gate 2>&1)
default_rc=$?
set -e
[[ "$default_rc" -eq 2 ]] || fail "default gate should be incomplete because real stages are not run"
assert_contains "$default_output" 'BASIC_LOCAL_GATE=PASS'
assert_contains "$default_output" 'REAL_PROFILE=NOT_RUN'
assert_contains "$default_output" 'REAL_NOTIFICATION=NOT_RUN'
assert_contains "$default_output" 'BASIC_CAPABILITY_ACCEPTANCE=INCOMPLETE'
assert_not_contains "$default_output" 'sk-test-'
fake_output=$(<"$FAKE_LOG")
assert_contains "$fake_output" 'deepseek=unset'
assert_contains "$fake_output" 'opencode=unset'

: >"$FAKE_LOG"
set +e
web_local_output=$(DEEPSEEK_API_KEY=sk-test-deepseek OPENCODE_API_KEY=sk-test-opencode run_gate --scope web-local 2>&1)
web_local_rc=$?
set -e
[[ "$web_local_rc" -eq 2 ]] || fail "web-local gate should be incomplete because real stages are not run"
assert_contains "$web_local_output" 'Web shared provider-free batch E2E: PASS'
assert_contains "$web_local_output" 'Web maintenance read-only three-viewport E2E: PASS'
assert_contains "$web_local_output" 'BASIC_CAPABILITY_SCOPE=web-local'
web_local_log=$(<"$FAKE_LOG")
assert_contains "$web_local_log" 'suite=batch'
assert_not_contains "$web_local_output" 'sk-test-'

set +e
missing_database_output=$(
  SIDERA_TEST_DATABASE_URL='' \
  SIDERA_KNOWLEDGE_TEST_DATABASE_URL='' \
  run_gate --scope web-local 2>&1
)
missing_database_rc=$?
set -e
[[ "$missing_database_rc" -eq 1 ]] || fail "missing databases should fail the local gate"
assert_contains "$missing_database_output" 'BASIC_DATABASE_CHECK=FAIL'
assert_contains "$missing_database_output" 'BASIC_LOCAL_GATE=FAIL'

set +e
missing_n1_output=$(SIDERA_N1_AUDIT_ROOT="$TEMP_DIR/missing-n1" run_gate --scope web-local 2>&1)
missing_n1_rc=$?
set -e
[[ "$missing_n1_rc" -eq 1 ]] || fail "missing N-1 audit source should fail the local gate"
assert_contains "$missing_n1_output" 'BASIC_N1_AUDIT_CHECK=FAIL'
assert_contains "$missing_n1_output" 'BASIC_LOCAL_GATE=FAIL'

set +e
missing_browser_output=$(FAKE_BROWSER_MODE=missing run_gate --scope web-local 2>&1)
missing_browser_rc=$?
set -e
[[ "$missing_browser_rc" -eq 1 ]] || fail "missing maintenance browser should fail the local gate"
assert_contains "$missing_browser_output" 'Web maintenance read-only three-viewport E2E: FAIL'
assert_contains "$missing_browser_output" 'BASIC_LOCAL_GATE=FAIL'

set +e
zero_output=$(FAKE_PYTEST_MODE=zero run_gate 2>&1)
zero_rc=$?
set -e
[[ "$zero_rc" -eq 1 ]] || fail "zero-test gate should fail"
assert_contains "$zero_output" 'BASIC_LOCAL_GATE=FAIL'
assert_contains "$zero_output" 'zero or skipped-only tests'
assert_contains "$zero_output" 'BASIC_CAPABILITY_ACCEPTANCE=FAIL'

set +e
skip_output=$(FAKE_PYTEST_MODE=skip run_gate 2>&1)
skip_rc=$?
set -e
[[ "$skip_rc" -eq 1 ]] || fail "skip-only gate should fail"
assert_contains "$skip_output" 'zero or skipped-only tests'

REAL_MANIFEST="$TEMP_DIR/manifest.json"
printf '%s\n' '{"run_id":"not-a-uuid","local_gate_record":"","profiles":[],"scenarios":{}}' >"$REAL_MANIFEST"
set +e
real_output=$(FAKE_REAL_CONFIG=pass SIDERA_RUN_REAL_BASIC=1 run_gate --manifest "$REAL_MANIFEST" 2>&1)
real_rc=$?
set -e
[[ "$real_rc" -eq 2 ]] || fail "unknown real run should be incomplete"
assert_contains "$real_output" 'REAL_PROFILE_CONFIG=PASS'
assert_contains "$real_output" 'REAL_PROFILE=INCOMPLETE'
assert_contains "$real_output" 'BASIC_CAPABILITY_ACCEPTANCE=INCOMPLETE'

STALE_RECORD="$TEMP_DIR/stale-gate.json"
printf '%s\n' '{"status":"PASS","dirty_scope":"clean","implementation_fingerprint":"old-fingerprint"}' >"$STALE_RECORD"
printf '%s\n' '{"run_id":"10000000-0000-0000-0000-000000000001","local_gate_record":"'"$STALE_RECORD"'"}' >"$REAL_MANIFEST"
set +e
stale_output=$(FAKE_REAL_CONFIG=pass SIDERA_REQUIRE_BASIC_LOCAL_RECORD=1 SIDERA_RUN_REAL_BASIC=1 run_gate --manifest "$REAL_MANIFEST" 2>&1)
stale_rc=$?
set -e
[[ "$stale_rc" -eq 2 ]] || fail "stale fingerprint should be incomplete"
assert_contains "$stale_output" 'REAL_PROFILE_CONFIG=INCOMPLETE'

PERSISTENT_EVIDENCE="$TEMP_DIR/persistent-evidence"
mkdir -p "$PERSISTENT_EVIDENCE"
printf '%s\n' '{"run_id":"10000000-0000-0000-0000-000000000001"}' >"$REAL_MANIFEST"
printf '%s\n' '{"run_id":"10000000-0000-0000-0000-000000000001","scenario":"profile","status":"PASS"}' \
  >"$PERSISTENT_EVIDENCE/profile.json"
set +e
malformed_output=$(
  FAKE_REAL_CONFIG=pass \
  FAKE_REAL_ACCEPTANCE_MODE=malformed \
  SIDERA_RUN_REAL_BASIC=1 \
  SIDERA_BASIC_ACCEPTANCE_EVIDENCE_DIR="$PERSISTENT_EVIDENCE" \
  run_gate --manifest "$REAL_MANIFEST" 2>&1
)
malformed_rc=$?
set -e
[[ "$malformed_rc" -eq 2 ]] || fail "malformed real evidence should be incomplete"
assert_contains "$malformed_output" 'REAL_PROFILE=INCOMPLETE'
preserved_evidence=$(<"$PERSISTENT_EVIDENCE/profile.json")
assert_contains "$preserved_evidence" '"status":"PASS"'

set +e
dirty_output=$(FAKE_GIT_SCOPE=dirty run_gate 2>&1)
dirty_rc=$?
set -e
[[ "$dirty_rc" -eq 1 ]] || fail "unprotected worktree changes should fail"
assert_contains "$dirty_output" 'BASIC_DIRTY_SCOPE=dirty'
assert_contains "$dirty_output" 'BASIC_LOCAL_GATE=FAIL'

printf '%s\n' 'basic capabilities gate tests passed'
