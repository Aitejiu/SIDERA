#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PREFLIGHT="$ROOT/scripts/r6-result-integration-preflight.sh"
DB_URL='postgresql+psycopg://sidera@127.0.0.1:55432/sidera_test'

fail() {
  printf 'r6 result preflight test failed: %s\n' "$1" >&2
  exit 1
}

if ! output=$(
  env -u SIDERA_RESULT_INTEGRATION_ENABLED \
    -u SIDERA_RESULT_MODULE_CATALOG \
    -u SIDERA_RESULT_CHECKS \
    -u SIDERA_RESULT_REPOSITORY_PATH \
    -u SIDERA_RESULT_BASE_COMMIT \
    SIDERA_DATABASE_URL="$DB_URL" "$PREFLIGHT"
); then
  fail "disabled result integration should be an acceptable preflight state"
fi
[[ "$output" == *'"enabled":false'* ]] || fail "disabled state was not reported"

if output=$(SIDERA_DATABASE_URL="$DB_URL" \
  SIDERA_RESULT_INTEGRATION_ENABLED=true "$PREFLIGHT" 2>&1); then
  fail "enabled result integration without declared modules should fail"
fi
[[ "$output" == *'"enabled":true'* ]] || fail "enabled state was not reported"
[[ "$output" != *'sk-'* ]] || fail "preflight output leaked a credential marker"

printf '%s\n' 'r6 result integration preflight tests passed'
