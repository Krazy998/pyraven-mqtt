#!/usr/bin/env python3
"""
Transfers data from a Rainforest Automation RAVEn USB stick to MQTT.

Topic layout (given --topic <prefix>):
  <prefix>/state              - birth/LWT messages ("online"/"offline"), retained
  <prefix>/sensor/telemetry   - telemetry payloads with an added "ts" field
  <prefix>/status             - reserved for heartbeat/status (not used yet)

Reliability:
- MQTT network loop thread with automatic reconnect backoff
- Exponential backoff + jitter on initial connect
- LWT + birth messages, with clean offline flush on shutdown
- Graceful shutdown on SIGINT/SIGTERM
- Optional TLS
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sys
import threading
import time
from typing import Any, Dict

import paho.mqtt.client as mqtt
import raven

DEFAULT_BACKOFF_MAX = 60
DEFAULT_KEEPALIVE = 30
DEFAULT_MAX_SERIAL_ERRORS = 10
SHUTDOWN_PUBLISH_TIMEOUT = 2.0


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    """Parse an integer environment variable, falling back to default if unset/empty."""
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    """Parse a float environment variable, falling back to default if unset/empty."""
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments with environment variable fallbacks."""
    parser = argparse.ArgumentParser(prog="pyrmqtt")
    parser.add_argument(
        "-d",
        "--device",
        default=os.getenv("RAVEN_DEVICE") or "/dev/ttyUSB0",
        help="Serial port of the USB stick [%(default)s]",
    )
    parser.add_argument(
        "-H",
        "--host",
        default=os.getenv("MQTT_HOST") or None,
        help="MQTT broker hostname (or MQTT_HOST env var)",
    )
    parser.add_argument(
        "-P",
        "--port",
        type=int,
        default=_env_int("MQTT_PORT", 1883),
        help="MQTT broker port [%(default)s]",
    )
    parser.add_argument(
        "-u",
        "--username",
        default=os.getenv("MQTT_USERNAME") or None,
        help="MQTT username (or MQTT_USERNAME env var)",
    )
    parser.add_argument(
        "-p",
        "--password",
        default=os.getenv("MQTT_PASSWORD") or None,
        help="MQTT password (prefer MQTT_PASSWORD env var to keep off argv)",
    )
    parser.add_argument(
        "-T",
        "--topic",
        default=os.getenv("MQTT_TOPIC") or "raven",
        help="MQTT topic prefix (e.g. 'raven') [%(default)s]",
    )
    parser.add_argument(
        "--qos",
        type=int,
        choices=(0, 1, 2),
        default=_env_int("MQTT_QOS", 1),
        help="MQTT QoS [%(default)s]",
    )
    parser.add_argument(
        "--retain",
        action="store_true",
        default=_env_bool("MQTT_RETAIN", False),
        help="Set retain flag on telemetry messages (default: off)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=_env_float("RAVEN_POLL_INTERVAL", 1.0),
        help="Seconds between polls [%(default)s]",
    )
    parser.add_argument(
        "--max-serial-errors",
        type=int,
        default=_env_int("RAVEN_MAX_SERIAL_ERRORS", DEFAULT_MAX_SERIAL_ERRORS),
        help="Exit after this many consecutive serial errors (0 disables) [%(default)s]",
    )
    parser.add_argument(
        "--client-id",
        default=os.getenv("MQTT_CLIENT_ID") or os.getenv("HOSTNAME", "pyrmqtt"),
        help="MQTT client id",
    )
    parser.add_argument(
        "--tls",
        action="store_true",
        default=_env_bool("MQTT_TLS", False),
        help="Enable TLS (or MQTT_TLS=true)",
    )
    parser.add_argument(
        "--cafile",
        default=os.getenv("MQTT_CAFILE") or None,
        help="CA file for TLS (or MQTT_CAFILE env var)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=_env_bool("MQTT_INSECURE", False),
        help="Skip TLS certificate verification (or MQTT_INSECURE=true)",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL") or "INFO",
        help="Logging level [%(default)s]",
    )

    args = parser.parse_args(argv)
    if not args.host:
        parser.error("MQTT broker host is required via --host or MQTT_HOST")
    if args.poll_interval < 0:
        parser.error("--poll-interval must be >= 0")
    if args.max_serial_errors < 0:
        parser.error("--max-serial-errors must be >= 0")
    return args


def setup_logging(level: str) -> None:
    """Configure root logger and tame noisy libs."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("paho").setLevel(logging.WARNING)


def build_topics(prefix: str) -> Dict[str, str]:
    """Build the topic map from a prefix."""
    base = prefix.rstrip("/")
    return {
        "state": f"{base}/state",
        "telemetry": f"{base}/sensor/telemetry",
        "status": f"{base}/status",
    }


def make_client(args: argparse.Namespace, topics: Dict[str, str]) -> mqtt.Client:
    """Create and configure the MQTT client, including LWT on <prefix>/state."""
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=args.client_id,
        clean_session=True,
    )
    if args.username:
        client.username_pw_set(args.username, args.password)
    if args.tls:
        if args.cafile:
            client.tls_set(ca_certs=args.cafile)
        else:
            client.tls_set()
        if args.insecure:
            client.tls_insecure_set(True)

    client.reconnect_delay_set(min_delay=1, max_delay=DEFAULT_BACKOFF_MAX)
    client.will_set(topics["state"], payload="offline", qos=1, retain=True)
    return client


def connect_with_backoff(
    client: mqtt.Client,
    host: str,
    port: int,
    stop_event: threading.Event,
) -> bool:
    """Connect to MQTT with exponential backoff + jitter, honoring stop requests."""
    log = logging.getLogger("pyrmqtt")
    backoff = 1.0
    while not stop_event.is_set():
        try:
            client.connect(host, port, keepalive=DEFAULT_KEEPALIVE)
            return True
        except OSError as err:
            delay = backoff + random.random()
            log.warning("MQTT connect error=%r backoff=%.1fs", err, delay)
            stop_event.wait(delay)
            backoff = min(backoff * 2, float(DEFAULT_BACKOFF_MAX))
    return False


def format_telemetry(raw: Any, now_ts: float | None = None) -> str:
    """Wrap a raw RAVEn reading with a timestamp and serialize as compact JSON."""
    ts = time.time() if now_ts is None else now_ts
    if isinstance(raw, dict):
        payload_obj: Dict[str, Any] = {"ts": ts, **raw}
    else:
        payload_obj = {"ts": ts, "data": raw}
    return json.dumps(payload_obj, separators=(",", ":"), default=str)


def publish_telemetry(
    client: mqtt.Client,
    *,
    topic: str,
    payload: str,
    qos: int,
    retain: bool,
) -> bool:
    """Publish a telemetry message; paho's background loop handles reconnects."""
    log = logging.getLogger("pyrmqtt")
    result = client.publish(topic, payload, qos=qos, retain=retain)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        log.warning("Publish failed rc=%s; paho network loop will retry connection", result.rc)
        return False
    return True


def _close_raven(raven_usb: Any) -> None:
    """Best-effort close of the RAVEn serial handle."""
    close_fn = getattr(raven_usb, "close", None)
    if callable(close_fn):
        try:
            close_fn()
            return
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    ser = getattr(raven_usb, "ser", None)
    ser_close = getattr(ser, "close", None)
    if callable(ser_close):
        try:
            ser_close()
        except Exception:  # pylint: disable=broad-exception-caught
            pass


def _shutdown_mqtt(client: mqtt.Client, state_topic: str, connected: bool) -> None:
    """Flush retained offline state before cleanly disconnecting MQTT."""
    if connected:
        try:
            info = client.publish(state_topic, "offline", qos=1, retain=True)
            info.wait_for_publish(timeout=SHUTDOWN_PUBLISH_TIMEOUT)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        try:
            client.disconnect()
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    client.loop_stop()


def _attach_callbacks(
    client: mqtt.Client,
    args: argparse.Namespace,
    state_topic: str,
) -> None:
    """Register MQTT lifecycle callbacks."""
    log = logging.getLogger("pyrmqtt")

    def on_connect(
        client_obj: mqtt.Client,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        """Publish birth message when connected."""
        if reason_code == 0:
            log.info("MQTT connected host=%s port=%s", args.host, args.port)
            client_obj.publish(state_topic, "online", qos=1, retain=True)
        else:
            log.error("MQTT connect failed rc=%s", reason_code)

    def on_disconnect(
        _client_obj: mqtt.Client,
        _userdata: Any,
        _disconnect_flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        """Log unexpected MQTT disconnects."""
        if reason_code != 0:
            log.warning("Unexpected MQTT disconnect rc=%s", reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect


def run_transfer_loop(
    raven_usb: Any,
    client: mqtt.Client,
    *,
    args: argparse.Namespace,
    telemetry_topic: str,
    stop_event: threading.Event,
) -> int:
    """Poll RAVEn and publish telemetry until stopped or serial errors exceed limit."""
    log = logging.getLogger("pyrmqtt")
    consecutive_errors = 0

    while not stop_event.is_set():
        try:
            raw = raven_usb.long_poll_result()
            consecutive_errors = 0
        except Exception:  # pylint: disable=broad-exception-caught
            consecutive_errors += 1
            log.exception(
                "Error polling RAVEn (consecutive=%d)",
                consecutive_errors,
            )
            if args.max_serial_errors and consecutive_errors >= args.max_serial_errors:
                log.error(
                    "Exceeded %d consecutive RAVEn errors; exiting for restart",
                    args.max_serial_errors,
                )
                return 3
            stop_event.wait(args.poll_interval)
            continue

        payload = format_telemetry(raw)
        publish_telemetry(
            client,
            topic=telemetry_topic,
            payload=payload,
            qos=args.qos,
            retain=args.retain,
        )
        stop_event.wait(args.poll_interval)

    return 0


def main() -> None:
    """Entry point: wire up device, MQTT, and transfer loop."""
    args = parse_args()
    setup_logging(args.log_level)
    log = logging.getLogger("pyrmqtt")
    topics = build_topics(args.topic)

    try:
        raven_usb = raven.raven.Raven(args.device)
        log.info("Connected to RAVEn device=%s", args.device)
    except Exception:  # pylint: disable=broad-exception-caught
        log.exception("Failed to connect to RAVEn device=%s", args.device)
        sys.exit(2)

    client = make_client(args, topics)
    _attach_callbacks(client, args, topics["state"])

    stop_event = threading.Event()

    def _stop_handler(*_args: Any) -> None:
        """Signal handler to request a graceful stop."""
        stop_event.set()

    signal.signal(signal.SIGINT, _stop_handler)
    signal.signal(signal.SIGTERM, _stop_handler)

    connected = False
    exit_code = 0
    try:
        connected = connect_with_backoff(client, args.host, args.port, stop_event)
        if connected:
            client.loop_start()
            exit_code = run_transfer_loop(
                raven_usb,
                client,
                args=args,
                telemetry_topic=topics["telemetry"],
                stop_event=stop_event,
            )
    finally:
        _shutdown_mqtt(client, topics["state"], connected)
        _close_raven(raven_usb)
        log.info("Stopped cleanly")

    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
