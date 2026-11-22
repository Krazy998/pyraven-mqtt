"""Unit tests for pyrmqtt.py."""

from __future__ import annotations

import json
import os
import sys
import threading
import types
import unittest
from unittest import mock


def _ensure_dependencies() -> None:
    """Import real third-party modules if installed, else install lightweight stubs."""
    try:
        import paho.mqtt.client  # noqa: F401  # pylint: disable=import-outside-toplevel,unused-import
    except ImportError:
        paho_mod = types.ModuleType("paho")
        mqtt_mod = types.ModuleType("paho.mqtt")
        client_mod = types.ModuleType("paho.mqtt.client")

        class CallbackAPIVersion:  # pylint: disable=too-few-public-methods
            """Stub enum for paho-mqtt v2."""

            VERSION2 = 2

        class StubClient:
            """Minimal stub for paho.mqtt.client.Client."""

            def __init__(self, **_kwargs):
                self.on_connect = None
                self.on_disconnect = None

            def username_pw_set(self, *_args, **_kwargs) -> None:
                """Stub auth setter."""

            def tls_set(self, *_args, **_kwargs) -> None:
                """Stub TLS setter."""

            def tls_insecure_set(self, *_args, **_kwargs) -> None:
                """Stub TLS insecure setter."""

            def reconnect_delay_set(self, *_args, **_kwargs) -> None:
                """Stub reconnect delay setter."""

            def will_set(self, *_args, **_kwargs) -> None:
                """Stub LWT setter."""

        client_mod.CallbackAPIVersion = CallbackAPIVersion
        client_mod.Client = StubClient
        client_mod.MQTT_ERR_SUCCESS = 0
        client_mod.MQTT_ERR_NO_CONN = 4
        mqtt_mod.client = client_mod
        paho_mod.mqtt = mqtt_mod
        sys.modules["paho"] = paho_mod
        sys.modules["paho.mqtt"] = mqtt_mod
        sys.modules["paho.mqtt.client"] = client_mod

    try:
        import raven  # noqa: F401  # pylint: disable=import-outside-toplevel,unused-import
    except ImportError:
        raven_pkg = types.ModuleType("raven")
        raven_sub = types.ModuleType("raven.raven")
        raven_sub.Raven = mock.MagicMock()
        raven_pkg.raven = raven_sub
        sys.modules["raven"] = raven_pkg
        sys.modules["raven.raven"] = raven_sub


_ensure_dependencies()

import pyrmqtt  # noqa: E402  # pylint: disable=wrong-import-position


class TestPyrmqtt(unittest.TestCase):
    """Tests for argument parsing, topics, telemetry formatting, and loop behavior."""

    def test_build_topics_strips_trailing_slash(self) -> None:
        topics = pyrmqtt.build_topics("home/raven/")
        self.assertEqual(
            topics,
            {
                "state": "home/raven/state",
                "telemetry": "home/raven/sensor/telemetry",
                "status": "home/raven/status",
            },
        )

    def test_parse_args_from_env(self) -> None:
        env = {
            "MQTT_HOST": "broker.local",
            "MQTT_PORT": "8883",
            "MQTT_USERNAME": "alice",
            "MQTT_PASSWORD": "secret",
            "MQTT_TOPIC": "meter",
            "MQTT_TLS": "true",
            "RAVEN_DEVICE": "/serial",
            "LOG_LEVEL": "DEBUG",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            args = pyrmqtt.parse_args([])
        self.assertEqual(args.host, "broker.local")
        self.assertEqual(args.port, 8883)
        self.assertEqual(args.username, "alice")
        self.assertEqual(args.password, "secret")
        self.assertEqual(args.topic, "meter")
        self.assertTrue(args.tls)
        self.assertEqual(args.device, "/serial")
        self.assertEqual(args.log_level, "DEBUG")

    def test_parse_args_cli_overrides_env(self) -> None:
        env = {"MQTT_HOST": "env-host", "MQTT_PORT": "1883", "RAVEN_DEVICE": "/serial"}
        with mock.patch.dict(os.environ, env, clear=True):
            args = pyrmqtt.parse_args(["-H", "cli-host", "-P", "1884", "-d", "/dev/ttyUSB1"])
        self.assertEqual(args.host, "cli-host")
        self.assertEqual(args.port, 1884)
        self.assertEqual(args.device, "/dev/ttyUSB1")

    def test_parse_args_requires_host(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                pyrmqtt.parse_args([])

    def test_make_client_initializes_v2(self) -> None:
        with mock.patch.dict(os.environ, {"MQTT_HOST": "broker.local"}, clear=True):
            args = pyrmqtt.parse_args([])
        topics = pyrmqtt.build_topics(args.topic)
        client = pyrmqtt.make_client(args, topics)
        self.assertIsNotNone(client)

    def test_format_telemetry_dict_and_scalar(self) -> None:
        encoded = pyrmqtt.format_telemetry({"demand": 1.25}, now_ts=1700000000.5)
        self.assertEqual(json.loads(encoded), {"ts": 1700000000.5, "demand": 1.25})

        encoded_scalar = pyrmqtt.format_telemetry("raw-line", now_ts=1700000001.0)
        self.assertEqual(json.loads(encoded_scalar), {"ts": 1700000001.0, "data": "raw-line"})

    def test_run_transfer_loop_exits_after_max_serial_errors(self) -> None:
        raven_usb = mock.MagicMock()
        raven_usb.long_poll_result.side_effect = RuntimeError("serial disconnected")
        client = mock.MagicMock()
        stop_event = threading.Event()
        args = mock.MagicMock(qos=1, retain=False, poll_interval=0.0, max_serial_errors=3)

        code = pyrmqtt.run_transfer_loop(
            raven_usb,
            client,
            args=args,
            telemetry_topic="raven/sensor/telemetry",
            stop_event=stop_event,
        )
        self.assertEqual(code, 3)
        self.assertEqual(raven_usb.long_poll_result.call_count, 3)

    def test_shutdown_flushes_offline_before_stopping_loop(self) -> None:
        client = mock.MagicMock()
        info = mock.MagicMock()
        client.publish.return_value = info
        call_order: list[str] = []
        info.wait_for_publish.side_effect = lambda **_kw: call_order.append("wait")
        client.disconnect.side_effect = lambda: call_order.append("disconnect")
        client.loop_stop.side_effect = lambda: call_order.append("loop_stop")

        pyrmqtt._shutdown_mqtt(client, "raven/state", connected=True)  # pylint: disable=protected-access
        client.publish.assert_called_once_with("raven/state", "offline", qos=1, retain=True)
        self.assertEqual(call_order, ["wait", "disconnect", "loop_stop"])


if __name__ == "__main__":
    unittest.main()
