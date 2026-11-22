#!/usr/bin/env bash
set -euo pipefail

pvoutput_flag="$(printf '%s' "${PVOUTPUT_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')"
case "$pvoutput_flag" in
  1|true|yes|on)
    pvoutput_enabled=true
    ;;
  *)
    pvoutput_enabled=false
    ;;
esac

if [ "$pvoutput_enabled" = "false" ]; then
  exec python /app/pyrmqtt.py "$@"
fi

python /app/pvoutput_uploader.py &
PV_PID=$!

python /app/pyrmqtt.py "$@" &
PYR_PID=$!

shutting_down=false

shutdown() {
  shutting_down=true
  kill -TERM "$PYR_PID" "$PV_PID" 2>/dev/null || true
  wait "$PYR_PID" 2>/dev/null || true
  wait "$PV_PID" 2>/dev/null || true
  exit 0
}

trap shutdown INT TERM

set +e
wait -n "$PYR_PID" "$PV_PID"
exit_code=$?
set -e

if [ "$shutting_down" = "true" ]; then
  exit 0
fi

# If either process exits unexpectedly, terminate the other so Docker restarts cleanly.
kill -TERM "$PYR_PID" "$PV_PID" 2>/dev/null || true
wait "$PYR_PID" 2>/dev/null || true
wait "$PV_PID" 2>/dev/null || true
exit "${exit_code:-1}"
