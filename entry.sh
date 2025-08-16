#!/usr/bin/env sh
set -eu

# Start with any args passed via `command:`
set -- "$@"

# --host
case " $* " in *" --host "*) : ;; *) [ -n "${MQTT_HOST:-}" ] && set -- --host "$MQTT_HOST" "$@";; esac
# --port
case " $* " in *" --port "*) : ;; *) [ -n "${MQTT_PORT:-}" ] && set -- --port "${MQTT_PORT}" "$@";; esac
# --device (default /serial)
: "${RAVEN_DEVICE:=/serial}"
case " $* " in *" --device "*) : ;; *) set -- --device "$RAVEN_DEVICE" "$@";; esac
# --username / --password (only if non-empty)
case " $* " in *" --username "*) : ;; *)
  if [ -n "${MQTT_USERNAME:-}" ]; then set -- --username "$MQTT_USERNAME" "$@"; fi
esac
case " $* " in *" --password "*) : ;; *)
  if [ -n "${MQTT_PASSWORD:-}" ]; then set -- --password "$MQTT_PASSWORD" "$@"; fi
esac

# Start PVOutput uploader if enabled
if [ "${PVOUTPUT_ENABLED:-false}" = "true" ]; then
  python /app/pvoutput_uploader.py &
fi

# Start RAVEn -> MQTT
exec python /app/pyrmqtt.py "$@"