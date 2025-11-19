"""PVOutput uploader worker.

Subscribes to MQTT telemetry from RAVEn, optionally fetches AC voltage from a
Fronius inverter, and posts status to PVOutput at a fixed interval.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import threading
import time
from datetime import datetime
from typing import Optional
from urllib import error as urlerror
from urllib import parse, request as urlrequest

from zoneinfo import ZoneInfo
import paho.mqtt.client as mqtt

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


class RequestException(Exception):
    """Raised when an HTTP request fails."""


class HttpResponse:
    """Minimal HTTP response wrapper mimicking requests.Response."""

    def __init__(self, status_code: int, content: bytes, headers: dict[str, str]):
        self.status_code = status_code
        self._content = content
        self.headers = headers
        self._text_cache: Optional[str] = None

    @property
    def text(self) -> str:
        """Return the body as text, caching the decoded value."""
        if self._text_cache is None:
            self._text_cache = self._content.decode("utf-8", errors="replace")
        return self._text_cache

    def json(self) -> dict:
        """Parse the response body as JSON and return the result."""
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        """Raise RequestException if the response code indicates an error."""
        if self.status_code >= 400:
            raise RequestException(f"HTTP {self.status_code}: {self.text}")


def _encode_basic_auth(auth: tuple[str, str]) -> str:
    user, password = auth
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _request(
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    data: Optional[dict] = None,
    headers: Optional[dict] = None,
    auth: Optional[tuple[str, str]] = None,
    timeout: Optional[float] = None,
) -> HttpResponse:
    query = parse.urlencode(params or {})
    full_url = f"{url}?{query}" if query else url

    req_headers = dict(headers or {})
    if auth:
        req_headers.setdefault("Authorization", _encode_basic_auth(auth))

    payload = None
    if data is not None:
        payload = parse.urlencode(data).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    req = urlrequest.Request(full_url, data=payload, headers=req_headers, method=method.upper())
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            content = resp.read()
            status = resp.getcode()
            resp_headers = dict(resp.getheaders())
    except urlerror.HTTPError as err:
        content = err.read()
        status = err.code
        resp_headers = dict(err.headers.items()) if err.headers else {}
    except (urlerror.URLError, socket.timeout, OSError) as err:
        raise RequestException(str(err)) from err
    return HttpResponse(status, content, resp_headers)


def http_get(url: str, *, params=None, auth=None, timeout=None) -> HttpResponse:
    """Perform a simple HTTP GET request using urllib primitives."""
    return _request("GET", url, params=params, auth=auth, timeout=timeout)


def http_post(url: str, *, data=None, headers=None, timeout=None) -> HttpResponse:
    """Perform an HTTP POST request with form data and optional headers."""
    return _request("POST", url, data=data, headers=headers, timeout=timeout)


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
        auth = (FRONIUS_USER, FRONIUS_PASS) if FRONIUS_USER and FRONIUS_PASS else None
        resp = http_get(url, params=params, auth=auth, timeout=FRONIUS_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return float(data["Body"]["Data"]["UAC"]["Value"])
    except (RequestException, ValueError, KeyError, TypeError):
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

    resp = http_post(PVOUTPUT_URL, data=payload, headers=headers, timeout=8)
    if resp.status_code != 200:
        raise RuntimeError(f"PVOutput HTTP {resp.status_code} {resp.text}")
    LOG.debug("PVOutput: %s | remaining=%s", resp.text.strip(), resp.headers.get("X-Rate-Limit-Remaining"))


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
        except (RequestException, RuntimeError) as err:
            LOG.warning("PVOutput post failed: %s", err)
        align_and_sleep(PVOUTPUT_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
