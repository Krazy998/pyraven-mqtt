"""Unit tests for pvoutput_uploader.py."""

from __future__ import annotations

from datetime import datetime
import os
import sys
import types
import unittest
from unittest import mock
from zoneinfo import ZoneInfo


def _ensure_paho() -> None:
    """Import real paho-mqtt if installed, else install a lightweight stub."""
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
                self.on_message = None

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
        client_mod.MQTTMessage = object
        client_mod.MQTT_ERR_SUCCESS = 0
        client_mod.MQTT_ERR_NO_CONN = 4
        mqtt_mod.client = client_mod
        paho_mod.mqtt = mqtt_mod
        sys.modules["paho"] = paho_mod
        sys.modules["paho.mqtt"] = mqtt_mod
        sys.modules["paho.mqtt.client"] = client_mod


_ensure_paho()

import pvoutput_uploader as pv  # noqa: E402  # pylint: disable=wrong-import-position


class TestPVOutputUploader(unittest.TestCase):
    """Tests for Latest state tracking, staleness, headers, and payload formatting."""

    def test_latest_raw_demand_and_demand_override(self) -> None:
        latest = pv.Latest()
        self.assertIsNone(latest.demand_watts(now_mono=10.0))

        latest.update_from_json({"raw_demand": 1450}, now_mono=10.0)
        self.assertEqual(latest.demand_watts(max_age_s=60, now_mono=20.0), 1450.0)

        # Later payload with only kW `demand` must replace earlier `raw_demand`
        latest.update_from_json({"demand": -0.75}, now_mono=25.0)
        self.assertEqual(latest.demand_watts(max_age_s=60, now_mono=30.0), -750.0)

        # Summation-only payload must NOT refresh timestamp or wipe demand
        latest.update_from_json({"summation_delivered": 12345.6}, now_mono=200.0)
        self.assertIsNone(latest.demand_watts(max_age_s=60, now_mono=200.0))

    def test_build_payload_gross_and_net_modes(self) -> None:
        now_local = datetime(2026, 11, 22, 14, 5, tzinfo=ZoneInfo("Australia/Melbourne"))

        gross_export = pv.build_pvoutput_payload(now_local, -520.4, 241.6, net_mode=0)
        self.assertEqual(
            gross_export,
            {"d": "20261122", "t": "14:05", "v4": "0", "n": "0", "v6": "242"},
        )

        net_export = pv.build_pvoutput_payload(now_local, -520.4, None, net_mode=1)
        self.assertEqual(
            net_export,
            {"d": "20261122", "t": "14:05", "v2": "520", "v4": "0", "n": "1"},
        )

        net_import = pv.build_pvoutput_payload(now_local, 1234.6, 238.2, net_mode=1)
        self.assertEqual(
            net_import,
            {"d": "20261122", "t": "14:05", "v2": "0", "v4": "1235", "n": "1", "v6": "238"},
        )

    def test_http_response_case_insensitive_headers(self) -> None:
        resp = pv.HttpResponse(200, b"OK 200", {"x-rate-limit-remaining": "59"})
        self.assertEqual(resp.get_header("X-Rate-Limit-Remaining"), "59")
        self.assertEqual(resp.get_header("X-Missing", "fallback"), "fallback")

    def test_load_config_and_make_mqtt_client(self) -> None:
        env = {"MQTT_TOPIC": "home/meter/", "PVOUTPUT_ENABLED": "TRUE", "TZ": "Australia/Melbourne"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = pv.load_config()
        self.assertTrue(cfg.pvoutput_enabled)
        self.assertEqual(cfg.mqtt_topic, "home/meter/sensor/telemetry")
        self.assertEqual(cfg.pvoutput_max_age_seconds, 600)
        client = pv.make_mqtt_client(cfg, pv.Latest())
        self.assertIsNotNone(client)


if __name__ == "__main__":
    unittest.main()
