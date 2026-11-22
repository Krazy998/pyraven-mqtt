"""PVOutput uploader worker.

Subscribes to MQTT telemetry from RAVEn, optionally fetches AC voltage from a
Fronius inverter, and posts status to PVOutput at a fixed interval.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
import random
import signal
import socket
import threading
import time
from typing import Any, Optional
from urllib import error as urlerror
from urllib import parse
from urllib import request as urlrequest
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt

LOG = logging.getLogger("pvoutput")

PVOUTPUT_URL = "https://pvoutput.org/service/r2/addstatus.jsp"
DEFAULT_BACKOFF_MAX = 60
DEFAULT_KEEPALIVE = 60


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


@dataclass(frozen=True)
class Config:
    """Runtime configuration loaded from environment variables."""

    log_level: str
    mqtt_host: str
    mqtt_port: int
    mqtt_user: Optional[str]
    mqtt_pass: Optional[str]
    mqtt_topic: str
    mqtt_tls: bool
    mqtt_cafile: Optional[str]
    mqtt_insecure: bool
    pvoutput_enabled: bool
    pvoutput_api_key: str
    pvoutput_system_id: str
    pvoutput_net: int
    pvoutput_interval_seconds: int
    pvoutput_max_age_seconds: int
    fronius_host: str
    fronius_device_id: str
    fronius_user: Optional[str]
    fronius_pass: Optional[str]
    fronius_timeout: float
    tz: ZoneInfo


def load_config() -> Config:
    """Load uploader configuration from environment variables."""
    interval = _env_int("PVOUTPUT_INTERVAL_SECONDS", 300)
    default_max_age = max(interval * 2, 120) if interval > 0 else 120
    topic = os.getenv("PVOUTPUT_MQTT_TOPIC", "").strip()
    if not topic:
        prefix = (os.getenv("MQTT_TOPIC") or "raven").rstrip("/")
        topic = f"{prefix}/sensor/telemetry"

    return Config(
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        mqtt_host=os.getenv("MQTT_HOST", "localhost"),
        mqtt_port=_env_int("MQTT_PORT", 1883),
        mqtt_user=os.getenv("MQTT_USERNAME", "") or None,
        mqtt_pass=os.getenv("MQTT_PASSWORD", "") or None,
        mqtt_topic=topic,
        mqtt_tls=_env_bool("MQTT_TLS", False),
        mqtt_cafile=os.getenv("MQTT_CAFILE", "") or None,
        mqtt_insecure=_env_bool("MQTT_INSECURE", False),
        pvoutput_enabled=_env_bool("PVOUTPUT_ENABLED", False),
        pvoutput_api_key=os.getenv("PVOUTPUT_API_KEY", "").strip(),
        pvoutput_system_id=os.getenv("PVOUTPUT_SYSTEM_ID", "").strip(),
        pvoutput_net=_env_int("PVOUTPUT_NET", 0),
        pvoutput_interval_seconds=interval,
        pvoutput_max_age_seconds=_env_int("PVOUTPUT_MAX_AGE_SECONDS", default_max_age),
        fronius_host=os.getenv("FRONIUS_HOST", "").strip(),
        fronius_device_id=os.getenv("FRONIUS_DEVICE_ID", "1").strip(),
        fronius_user=os.getenv("FRONIUS_USERNAME", "") or None,
        fronius_pass=os.getenv("FRONIUS_PASSWORD", "") or None,
        fronius_timeout=_env_float("FRONIUS_TIMEOUT", 3.0),
        tz=ZoneInfo(os.getenv("TZ", "Australia/Melbourne")),
    )


class Latest:
    """Thread-safe holder for the most recent demand reading."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ts: Optional[float] = None
        self._demand_w: Optional[float] = None

    def update_from_json(self, payload: dict[str, Any], *, now_mono: Optional[float] = None) -> None:
        """Update demand in watts from a telemetry JSON payload."""
        demand_w: Optional[float] = None
        if "raw_demand" in payload:
            try:
                demand_w = float(payload["raw_demand"])
            except (TypeError, ValueError):
                return
        elif "demand" in payload:
            try:
                demand_w = float(payload["demand"]) * 1000.0
            except (TypeError, ValueError):
                return
        else:
            # Summation or non-demand frame; leave last demand and timestamp untouched.
            return

        ts = time.monotonic() if now_mono is None else now_mono
        with self._lock:
            self._demand_w = demand_w
            self._ts = ts

    def demand_watts(
        self,
        *,
        max_age_s: Optional[float] = None,
        now_mono: Optional[float] = None,
    ) -> Optional[float]:
        """Return instantaneous demand in watts if present and not stale."""
        now = time.monotonic() if now_mono is None else now_mono
        with self._lock:
            if self._demand_w is None or self._ts is None:
                return None
            if max_age_s is not None and (now - self._ts) > max_age_s:
                return None
            return self._demand_w


class RequestException(Exception):
    """Raised when an HTTP request fails."""


class HttpResponse:
    """Minimal HTTP response wrapper with case-insensitive header lookup."""

    def __init__(self, status_code: int, content: bytes, headers: dict[str, str]):
        self.status_code = status_code
        self._content = content
        self.headers = {k.lower(): v for k, v in headers.items()}
        self._text_cache: Optional[str] = None

    @property
    def text(self) -> str:
        """Return the body as text, caching the decoded value."""
        if self._text_cache is None:
            self._text_cache = self._content.decode("utf-8", errors="replace")
        return self._text_cache

    def get_header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """Look up an HTTP response header case-insensitively."""
        return self.headers.get(name.lower(), default)

    def json(self) -> dict[str, Any]:
        """Parse the response body as JSON and return the result."""
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        """Raise RequestException if the response code indicates an error."""
        if self.status_code >= 400:
            raise RequestException(f"HTTP {self.status_code}: {self.text}")


def _encode_basic_auth(auth: tuple[str, str]) -> str:
    """Encode a username/password pair into an HTTP Basic Authorization header."""
    user, password = auth
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _request(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    data: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    auth: Optional[tuple[str, str]] = None,
    timeout: Optional[float] = None,
) -> HttpResponse:
    """Execute an HTTP request using urllib and return an HttpResponse."""
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
        try:
            content = err.read()
            status = err.code
            resp_headers = dict(err.headers.items()) if err.headers else {}
        finally:
            err.close()
    except (urlerror.URLError, socket.timeout, OSError) as err:
        raise RequestException(str(err)) from err
    return HttpResponse(status, content, resp_headers)


def http_get(
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    auth: Optional[tuple[str, str]] = None,
    timeout: Optional[float] = None,
) -> HttpResponse:
    """Perform a simple HTTP GET request using urllib primitives."""
    return _request("GET", url, params=params, auth=auth, timeout=timeout)


def http_post(
    url: str,
    *,
    data: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: Optional[float] = None,
) -> HttpResponse:
    """Perform an HTTP POST request with form data and optional headers."""
    return _request("POST", url, data=data, headers=headers, timeout=timeout)


def make_mqtt_client(cfg: Config, latest: Latest) -> mqtt.Client:
    """Create and configure the MQTT subscriber client."""
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    if cfg.mqtt_user:
        client.username_pw_set(cfg.mqtt_user, cfg.mqtt_pass)
    if cfg.mqtt_tls:
        if cfg.mqtt_cafile:
            client.tls_set(ca_certs=cfg.mqtt_cafile)
        else:
            client.tls_set()
        if cfg.mqtt_insecure:
            client.tls_insecure_set(True)

    client.reconnect_delay_set(min_delay=1, max_delay=DEFAULT_BACKOFF_MAX)

    def on_connect(
        client_obj: mqtt.Client,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        if reason_code == 0:
            LOG.info("Connected to MQTT; subscribing %s", cfg.mqtt_topic)
            client_obj.subscribe(cfg.mqtt_topic, qos=0)
        else:
            LOG.error("MQTT connect failed rc=%s", reason_code)

    def on_disconnect(
        _client_obj: mqtt.Client,
        _userdata: Any,
        _disconnect_flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        if reason_code != 0:
            LOG.warning("Unexpected MQTT disconnect rc=%s", reason_code)

    def on_message(_client_obj: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            if isinstance(payload, dict):
                latest.update_from_json(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            LOG.warning("Bad MQTT payload on %s", msg.topic)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return client


def connect_with_backoff(
    client: mqtt.Client,
    cfg: Config,
    stop_event: threading.Event,
) -> bool:
    """Connect to MQTT with exponential backoff + jitter, honoring stop requests."""
    backoff = 1.0
    while not stop_event.is_set():
        try:
            client.connect(cfg.mqtt_host, cfg.mqtt_port, keepalive=DEFAULT_KEEPALIVE)
            return True
        except OSError as err:
            delay = backoff + random.random()
            LOG.warning("MQTT connect error=%r backoff=%.1fs", err, delay)
            stop_event.wait(delay)
            backoff = min(backoff * 2, float(DEFAULT_BACKOFF_MAX))
    return False


def get_voltage(cfg: Config) -> Optional[float]:
    """Fetch AC voltage (UAC) from Fronius CommonInverterData."""
    if not cfg.fronius_host:
        return None
    url = f"http://{cfg.fronius_host}/solar_api/v1/GetInverterRealtimeData.cgi"
    params = {
        "Scope": "Device",
        "DeviceId": cfg.fronius_device_id,
        "DataCollection": "CommonInverterData",
    }
    try:
        auth = (cfg.fronius_user, cfg.fronius_pass) if cfg.fronius_user and cfg.fronius_pass else None
        resp = http_get(url, params=params, auth=auth, timeout=cfg.fronius_timeout)
        resp.raise_for_status()
        data = resp.json()
        return float(data["Body"]["Data"]["UAC"]["Value"])
    except (RequestException, ValueError, KeyError, TypeError):
        LOG.debug("Voltage fetch failed", exc_info=False)
        return None


def build_pvoutput_payload(
    now_local: datetime,
    demand_w: float,
    voltage_v: Optional[float],
    net_mode: int,
) -> dict[str, str]:
    """Build the form payload for PVOutput addstatus.jsp."""
    if net_mode == 0:
        import_w = 0 if demand_w < 0 else int(round(demand_w))
        payload = {
            "d": now_local.strftime("%Y%m%d"),
            "t": now_local.strftime("%H:%M"),
            "v4": str(import_w),
            "n": "0",
        }
    else:
        imp = int(round(demand_w)) if demand_w >= 0 else 0
        exp = int(round(abs(demand_w))) if demand_w < 0 else 0
        payload = {
            "d": now_local.strftime("%Y%m%d"),
            "t": now_local.strftime("%H:%M"),
            "v2": str(exp),
            "v4": str(imp),
            "n": "1",
        }

    if voltage_v is not None:
        payload["v6"] = str(int(round(voltage_v)))
    return payload


def post_pvoutput(
    cfg: Config,
    now_local: datetime,
    demand_w: float,
    voltage_v: Optional[float],
) -> None:
    """POST a status update to PVOutput."""
    payload = build_pvoutput_payload(now_local, demand_w, voltage_v, cfg.pvoutput_net)
    headers = {
        "X-Pvoutput-Apikey": cfg.pvoutput_api_key,
        "X-Pvoutput-SystemId": cfg.pvoutput_system_id,
        "X-Rate-Limit": "1",
    }

    resp = http_post(PVOUTPUT_URL, data=payload, headers=headers, timeout=8)
    if resp.status_code != 200:
        raise RuntimeError(f"PVOutput HTTP {resp.status_code} {resp.text}")
    LOG.debug(
        "PVOutput: %s | remaining=%s",
        resp.text.strip(),
        resp.get_header("X-Rate-Limit-Remaining"),
    )


def align_and_sleep(interval_s: int, stop_event: threading.Event) -> None:
    """Wait until the next interval boundary (e.g., 5-min grid), waking on stop."""
    now = time.time()
    next_tick = (int(now // interval_s) + 1) * interval_s
    stop_event.wait(max(0.0, next_tick - now))


def main() -> None:
    """Entry point."""
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("paho").setLevel(logging.WARNING)

    if not cfg.pvoutput_enabled:
        LOG.info("PVOutput uploader disabled (PVOUTPUT_ENABLED=false)")
        return
    if not cfg.pvoutput_api_key or not cfg.pvoutput_system_id:
        LOG.error("PVOUTPUT_API_KEY or PVOUTPUT_SYSTEM_ID missing; cannot upload.")
        return
    if cfg.pvoutput_interval_seconds <= 0:
        LOG.error("PVOUTPUT_INTERVAL_SECONDS must be > 0 (got %s)", cfg.pvoutput_interval_seconds)
        return

    stop_event = threading.Event()

    def _stop_handler(*_args: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _stop_handler)
    signal.signal(signal.SIGTERM, _stop_handler)

    latest = Latest()
    client = make_mqtt_client(cfg, latest)
    connected = False

    try:
        connected = connect_with_backoff(client, cfg, stop_event)
        if not connected:
            return
        client.loop_start()
        LOG.info(
            "PVOutput uploader running: interval=%ss, net=%s, max_age=%ss, topic=%s",
            cfg.pvoutput_interval_seconds,
            cfg.pvoutput_net,
            cfg.pvoutput_max_age_seconds,
            cfg.mqtt_topic,
        )

        align_and_sleep(cfg.pvoutput_interval_seconds, stop_event)
        while not stop_event.is_set():
            now_local = datetime.now(cfg.tz)
            demand_w = latest.demand_watts(max_age_s=cfg.pvoutput_max_age_seconds)
            if demand_w is None:
                LOG.warning("No recent demand telemetry; skipping PVOutput upload")
            else:
                voltage_v = get_voltage(cfg)
                try:
                    post_pvoutput(cfg, now_local, demand_w, voltage_v)
                except (RequestException, RuntimeError) as err:
                    LOG.warning("PVOutput post failed: %s", err)
            align_and_sleep(cfg.pvoutput_interval_seconds, stop_event)
    finally:
        if connected:
            try:
                client.disconnect()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
        client.loop_stop()
        LOG.info("PVOutput uploader stopped cleanly")


if __name__ == "__main__":
    main()
