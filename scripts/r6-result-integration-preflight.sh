#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
CONTROL_PLANE_ROOT="$REPO_ROOT/services/control-plane"
OUTPUT_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sidera-r6-result-preflight.XXXXXX")
trap 'rm -rf "$OUTPUT_DIR"' EXIT

if [[ -n "${SIDERA_R6_RESULT_PYTHON_BIN:-}" ]]; then
  PYTHON_COMMAND=("$SIDERA_R6_RESULT_PYTHON_BIN")
else
  PYTHON_COMMAND=(uv run --project "$CONTROL_PLANE_ROOT" python)
fi

READINESS_ARGUMENTS=()
if [[ "${SIDERA_RESULT_REAL_GATE_PREFLIGHT:-0}" == "1" ]]; then
  READINESS_ARGUMENTS+=(--real-gate)
fi

set +e
(
  cd "$REPO_ROOT"
  if ((${#READINESS_ARGUMENTS[@]} == 0)); then
    PYTHONPATH="$CONTROL_PLANE_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
      "${PYTHON_COMMAND[@]}" -m sidera_control_plane.result_readiness
  else
    PYTHONPATH="$CONTROL_PLANE_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
      "${PYTHON_COMMAND[@]}" -m sidera_control_plane.result_readiness \
      "${READINESS_ARGUMENTS[@]}"
  fi
) >"$OUTPUT_DIR/stdout" 2>"$OUTPUT_DIR/stderr"
STATUS=$?
set -e

case "$STATUS" in
  0|2)
    cat "$OUTPUT_DIR/stdout"
    exit "$STATUS"
    ;;
  *)
    printf '%s\n' '{"status":"error","error_code":"result_preflight_failed"}' >&2
    exit 1
    ;;
esac
