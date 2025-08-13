#!/usr/bin/env python3
"""
Transfers data from a Rainforest Automation RAVEn USB stick to an MQTT topic.

- Payload and topic are unchanged vs original (telemetry is json.dumps(long_poll_result()))
- Adds robust reconnects, MQTT loop, LWT/birth, and graceful shutdown.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sys
import time
from typing import Any

import paho.mqtt.client as mqtt  # pip install paho-mqtt
import raven  # pip install pyraven

DEFAULT_BACKOFF_MAX = 60
DEFAULT_KEEPALIVE = 30


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(prog="pyrmqtt")
    parser.add_argument(
        "-d",
        "--device",
        default="/dev/ttyUSB0",
        help="Serial port of the USB stick [%(default)s]",
    )
    parser.add_argument("-H", "--host", required=True, help="MQTT broker hostname")
    parser.add_argument(
        "-P",
        "--port",
        type=int,
        default=1883,
        help="MQTT broker port [%(default)s]",
    )
    parser.add_argument("-u", "--username", help="MQTT username")
    parser.add_argument("-p", "--password", help="MQTT password")
    parser.add_argument(
        "-T",
        "--topic",
        default="raven",
        help="MQTT topic to publish to [%(default)s]",
    )
    parser.add_argument(
        "--qos", type=int, choices=(0, 1, 2), default=0, help="MQTT QoS [%(default)s]"
    )
    parser.add_argument(
        "--retain",
        action="store_true",
        help="Set retain flag on telemetry messages",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Seconds between polls [%(default)s]",
    )
    parser.add_argument(
        "--client-id", default=os.getenv("HOSTNAME", "pyrmqtt"), help="MQTT client id"
    )
    parser.add_argument("--tls", action="store_true", help="Enable TLS")
    parser.add_argument("--cafile", help="CA file for TLS")
    parser.add_argument(
        "--insecure", action="store_true", help="Skip TLS certificate verification"
    )
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    return parser.parse_args()


def setup_logging(level: str) -> None:
    """Configure root logger and tame noisy libs."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("paho").setLevel(logging.WARNING)


def make_client(args: argparse.Namespace) -> mqtt.Client:
    """Create and configure the MQTT client, including LWT."""
    client = mqtt.Client(client_id=args.client_id, clean_session=True)
    if args.username:
        client.username_pw_set(args.username, args.password)
    if args.tls:
        if args.cafile:
            client.tls_set(ca_certs=args.cafile)
        else:
            client.tls_set()  # system defaults
        if args.insecure:
            client.tls_insecure_set(True)

    # Last Will: publish "offline" if we disappear
    client.will_set(args.topic, payload="offline", qos=1, retain=True)
    return client


def connect_with_backoff(
    client: mqtt.Client, host: str, port: int, stopping_flag: list[bool]
) -> None:
    """Connect to MQTT with exponential backoff + jitter, honoring stop requests."""
    log = logging.getLogger("pyrmqtt")
    backoff = 1.0
    while not stopping_flag[0]:
        try:
            client.connect(host, port, keepalive=DEFAULT_KEEPALIVE)
            return
        except OSError as err:  # network/socket errors
            log.warning("MQTT connect error=%r backoff=%.1fs", err, backoff)
            time.sleep(backoff + random.random())
            backoff = min(backoff * 2, float(DEFAULT_BACKOFF_MAX))


def publish_with_reconnect(
    client: mqtt.Client,
    topic: str,
    payload: str,
    qos: int,
    retain: bool,
    stopping_flag: list[bool],
) -> None:
    """Publish once; on failure, attempt to reconnect with backoff and retry birth."""
    log = logging.getLogger("pyrmqtt")
    result = client.publish(topic, payload, qos=qos, retain=retain)
    if result.rc == mqtt.MQTT_ERR_SUCCESS:
        return

    log.warning("Publish failed rc=%s; attempting reconnect", result.rc)
    backoff = 1.0
    while not stopping_flag[0]:
        try:
            client.reconnect()
            log.info("MQTT reconnected")
            # Birth message: let subscribers know we're back
            client.publish(topic, "online", qos=1, retain=True)
            # try publish again
            retry = client.publish(topic, payload, qos=qos, retain=retain)
            if retry.rc == mqtt.MQTT_ERR_SUCCESS:
                return
        except OSError as err:
            log.warning("Reconnect error=%r backoff=%.1fs", err, backoff)
            time.sleep(backoff + random.random())
            backoff = min(backoff * 2, float(DEFAULT_BACKOFF_MAX))


def main() -> None:
    """Entry point: wire up device, MQTT, and transfer loop."""
    args = parse_args()
    setup_logging(args.log_level)
    log = logging.getLogger("pyrmqtt")

    # Connect to RAVEn
    try:
        raven_usb = raven.raven.Raven(args.device)
        log.info("Connected to RAVEn device=%s", args.device)
    except Exception:  # pylint: disable=broad-except
        # The library may raise non-standard exceptions; log full trace and exit.
        log.exception("Failed to connect to RAVEn device=%s", args.device)
        sys.exit(2)

    client = make_client(args)

    # Stop flag in a list so closures can mutate it
    stopping = [False]

    def _stop_handler(*_args: Any) -> None:
        """Signal handler to request a graceful stop."""
        stopping[0] = True

    signal.signal(signal.SIGINT, _stop_handler)
    signal.signal(signal.SIGTERM, _stop_handler)

    def on_connect(
        client_obj: mqtt.Client,
        _userdata: Any,
        _flags: dict[str, Any],
        return_code: int,  # return code from the broker
        _properties: Any = None,
    ) -> None:
        """MQTT on_connect callback: publish birth if connection OK."""
        if return_code == 0:
            log.info("MQTT connected host=%s port=%s", args.host, args.port)
            client_obj.publish(args.topic, "online", qos=1, retain=True)
        else:
            log.error("MQTT connect failed rc=%s", return_code)

    def on_disconnect(
        _client_obj: mqtt.Client,
        _userdata: Any,
        return_code: int,  # disconnect return code
        _properties: Any = None,
    ) -> None:
        """MQTT on_disconnect callback: note unexpected disconnects."""
        if return_code != 0:
            log.warning("Unexpected MQTT disconnect rc=%s", return_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    client.loop_start()
    connect_with_backoff(client, args.host, args.port, stopping)

    # Transfer loop
    while not stopping[0]:
        try:
            payload = json.dumps(raven_usb.long_poll_result())
        except Exception:  # pylint: disable=broad-except
            # pyraven may raise broad exceptions on serial hiccups; log and continue.
            log.exception("Error polling RAVEn")
            time.sleep(args.poll_interval)
            continue

        publish_with_reconnect(
            client,
            topic=args.topic,
            payload=payload,
            qos=args.qos,
            retain=args.retain,
            stopping_flag=stopping,
        )

        time.sleep(args.poll_interval)

    # Shutdown
    try:
        client.publish(args.topic, "offline", qos=1, retain=True)
    except Exception:  # pylint: disable=broad-except
        pass
    client.loop_stop()
    client.disconnect()
    log.info("Stopped cleanly")


if __name__ == "__main__":
    main()
