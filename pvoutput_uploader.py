"""PVOutput uploader worker.

Subscribes to MQTT telemetry from RAVEn, optionally fetches AC voltage from a
Fronius inverter, and posts status to PVOutput at a fixed interval.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional

from zoneinfo import ZoneInfo
import paho.mqtt.client as mqtt
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("pvoutput")

# ---------- Config via env ----------
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USERNAME", "") or None
MQTT_PASS = os.getenv("MQTT_PASSWORD", "") or None
MQTT_TOPIC = os.getenv("PVOUTPUT_MQTT_TOPIC", "raven/sensor/telemetry")

PVOUTPUT_ENABLED = os.getenv("PVOUTPUT_ENABLED", "false").lower() == "true"
PVOUTPUT_API_KEY = os.getenv("PVOUTPUT_API_KEY", "")
PVOUTPUT_SYSTEM_ID = os.getenv("PVOUTPUT_SYSTEM_ID", "")
PVOUTPUT_NET = int(os.getenv("PVOUTPUT_NET", "0"))  # 0=gross (v4), 1=net (v2/v4)
PVOUTPUT_INTERVAL_SECONDS = int(os.getenv("PVOUTPUT_INTERVAL_SECONDS", "300"))
PVOUTPUT_URL = "https://pvoutput.org/service/r2/addstatus.jsp"

FRONIUS_HOST = os.getenv("FRONIUS_HOST", "")
FRONIUS_DEVICE_ID = os.getenv("FRONIUS_DEVICE_ID", "1")
FRONIUS_USER = os.getenv("FRONIUS_USERNAME", "") or None
FRONIUS_PASS = os.getenv("FRONIUS_PASSWORD", "") or None
FRONIUS_TIMEOUT = float(os.getenv("FRONIUS_TIMEOUT", "3.0"))

TZ = ZoneInfo(os.getenv("TZ", "Australia/Melbourne"))


class Latest:
    """Thread-safe holder for the most recent demand reading."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ts = 0.0
        self._raw_demand_w: Optional[float] = None
        self._demand_kw: Optional[float] = None

    def update_from_json(self, payload: dict) -> None:
        """Update from a telemetry JSON payload."""
        with self._lock:
            if "raw_demand" in payload:
                try:
                    self._raw_demand_w = float(payload["raw_demand"])
                except (TypeError, ValueError):
                    self._raw_demand_w = None
            elif "demand" in payload:
                try:
                    self._demand_kw = float(payload["demand"])
                except (TypeError, ValueError):
                    self._demand_kw = None
            self._ts = time.time()

    def demand_watts(self) -> Optional[float]:
        """Return instantaneous demand in watts, if known."""
        with self._lock:
            if self._raw_demand_w is not None:
                return float(self._raw_demand_w)
            if self._demand_kw is not None:
                return float(self._demand_kw) * 1000.0
            return None


LATEST = Latest()


def on_connect(client: mqtt.Client, _userdata, _flags, result_code: int) -> None:
    """MQTT connect callback."""
    if result_code == 0:
        LOG.info("Connected to MQTT; subscribing %s", MQTT_TOPIC)
        client.subscribe(MQTT_TOPIC, qos=0)
    else:
        LOG.error("MQTT connect failed rc=%s", result_code)


def on_message(_client: mqtt.Client, _userdata, msg: mqtt.MQTTMessage) -> None:
    """MQTT message callback: parse JSON and update state."""
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
        if isinstance(payload, dict):
            LATEST.update_from_json(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        LOG.warning("Bad MQTT payload on %s", msg.topic)


def start_mqtt() -> None:
    """Start MQTT client in a background thread."""
    client = mqtt.Client()
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    threading.Thread(target=client.loop_forever, name="mqtt-loop", daemon=True).start()


def get_voltage() -> Optional[float]:
    """Fetch AC voltage (UAC) from Fronius CommonInverterData."""
    if not FRONIUS_HOST:
        return None
    url = f"http://{FRONIUS_HOST}/solar_api/v1/GetInverterRealtimeData.cgi"
    params = {"Scope": "Device", "DeviceId": FRONIUS_DEVICE_ID, "DataCollection": "CommonInverterData"}
    try:
        query = urlparse.urlencode(params)
        full_url = f"{url}?{query}"
        req = urlrequest.Request(full_url)
        if FRONIUS_USER and FRONIUS_PASS:
            credentials = f"{FRONIUS_USER}:{FRONIUS_PASS}".encode("utf-8")
            token = base64.b64encode(credentials).decode("ascii")
            req.add_header("Authorization", f"Basic {token}")
        with urlrequest.urlopen(req, timeout=FRONIUS_TIMEOUT) as resp:
            data = json.load(resp)
        return float(data["Body"]["Data"]["UAC"]["Value"])
    except (urlerror.URLError, ValueError, KeyError, TypeError):
        LOG.debug("Voltage fetch failed", exc_info=False)
        return None


def post_pvoutput(now_local: datetime, demand_w: Optional[float], voltage_v: Optional[float]) -> None:
    """POST a status update to PVOutput."""
    if PVOUTPUT_NET == 0:
        import_w = 0 if demand_w is None else (0 if demand_w < 0 else int(round(demand_w)))
        payload = {
            "d": now_local.strftime("%Y%m%d"),
            "t": now_local.strftime("%H:%M"),
            "v4": str(import_w),
            "n": "0",
        }
    else:
        # Net mode: v2=export, v4=import, n=1
        imp = 0
        exp = 0
        if demand_w is not None:
            if demand_w >= 0:
                imp = int(round(demand_w))
            else:
                exp = int(round(abs(demand_w)))
        payload = {
            "d": now_local.strftime("%Y%m%d"),
            "t": now_local.strftime("%H:%M"),
            "v2": str(exp),
            "v4": str(imp),
            "n": "1",
        }

    if voltage_v is not None:
        payload["v6"] = str(int(round(voltage_v)))

    headers = {
        "X-Pvoutput-Apikey": PVOUTPUT_API_KEY,
        "X-Pvoutput-SystemId": PVOUTPUT_SYSTEM_ID,
        "X-Rate-Limit": "1",
    }

    data = urlparse.urlencode(payload).encode("ascii")
    req = urlrequest.Request(PVOUTPUT_URL, data=data, headers=headers)
    try:
        with urlrequest.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
            remaining = resp.headers.get("X-Rate-Limit-Remaining")
        LOG.debug("PVOutput: %s | remaining=%s", body, remaining)
    except urlerror.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"PVOutput HTTP {err.code} {body}") from err
    except urlerror.URLError as err:
        raise RuntimeError(f"PVOutput request failed: {err}") from err


def align_and_sleep(interval_s: int) -> None:
    """Sleep so runs align to the interval boundary (e.g., 5-min grid)."""
    now = time.time()
    next_tick = (int(now // interval_s) + 1) * interval_s
    time.sleep(max(0, next_tick - now))


def main() -> None:
    """Entry point."""
    if not PVOUTPUT_ENABLED:
        LOG.info("PVOutput uploader disabled (PVOUTPUT_ENABLED=false)")
        return
    if not PVOUTPUT_API_KEY or not PVOUTPUT_SYSTEM_ID:
        LOG.error("PVOUTPUT_* env missing; cannot upload.")
        return

    start_mqtt()
    LOG.info(
        "PVOutput uploader running: interval=%ss, net=%s, topic=%s",
        PVOUTPUT_INTERVAL_SECONDS,
        PVOUTPUT_NET,
        MQTT_TOPIC,
    )

    align_and_sleep(PVOUTPUT_INTERVAL_SECONDS)
    while True:
        now_local = datetime.now(TZ)
        demand_w = LATEST.demand_watts()
        voltage_v = get_voltage()
        try:
            post_pvoutput(now_local, demand_w, voltage_v)
        except (RuntimeError, urlerror.URLError) as err:
            LOG.warning("PVOutput post failed: %s", err)
        align_and_sleep(PVOUTPUT_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
