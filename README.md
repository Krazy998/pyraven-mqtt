# pyraven-mqtt

Publish real-time smart-meter telemetry from a **Rainforest RAVEn** Zigbee USB to **MQTT** (for Home Assistant or any MQTT consumer).  
Optionally, the same container can also **upload status to PVOutput** and (optionally) fetch **voltage** from a **Fronius** inverter.

- Core service: `pyrmqtt.py` → RAVEn ➜ MQTT  
- Optional worker: `pvoutput_uploader.py` → MQTT (+Fronius voltage) ➜ PVOutput  

---

## Features

- Streams smart-meter data from RAVEn to MQTT (`raven/sensor/telemetry`)
- Robust MQTT reconnect & backoff
- Optional PVOutput uploads on a fixed interval (default: 5 min)
- Optional Fronius voltage (CommonInverterData → `UAC`)

---

## How it works (high level)

```
RAVEn USB  ──>  pyrmqtt.py  ── MQTT publish ──>  Broker (Home Assistant, etc.)

                  ↑
                  └── pvoutput_uploader.py (optional)
                        ├─ subscribes to MQTT telemetry
                        ├─ queries Fronius voltage (optional)
                        └─ posts to PVOutput on interval
```

---

## Quick start (Docker Compose)

> **Tip:** Map your RAVEn by stable path (e.g., `/dev/serial/by-id/...`) to avoid tty name changes.

```yaml
services:
  pyrmqtt:
    image: your/pyraven-mqtt:latest
    container_name: pyrmqtt
    restart: always

    # Map the RAVEn device into the container
    devices:
      - /dev/serial/by-id/usb-Rainforest_RFA-Z106-RA-PC_RAVEn_v2.3.21-if00-port0:/serial

    environment:
      # --- MQTT (required) ---
      MQTT_HOST: "192.168.0.0"
      MQTT_PORT: "1883"
      MQTT_USERNAME: "youruser"
      MQTT_PASSWORD: "yourpass"

      # --- RAVEn device (optional; defaults to /serial) ---
      RAVEN_DEVICE: "/serial"

      # --- PVOutput uploader (optional) ---
      PVOUTPUT_ENABLED: "false"         # set "true" to enable
      PVOUTPUT_API_KEY: ""              # required if enabled
      PVOUTPUT_SYSTEM_ID: ""            # required if enabled
      PVOUTPUT_NET: "0"                 # 0=gross (v4 import only), 1=net (v2 export + v4 import)
      PVOUTPUT_INTERVAL_SECONDS: "300"  # 5 minutes
      PVOUTPUT_MQTT_TOPIC: "raven/sensor/telemetry"

      # --- Fronius voltage (optional) ---
      FRONIUS_HOST: "192.168.0.0"
      FRONIUS_DEVICE_ID: "1"
      # FRONIUS_USERNAME: ""
      # FRONIUS_PASSWORD: ""
      FRONIUS_TIMEOUT: "3.0"

      # --- Misc ---
      TZ: "Australia/Melbourne"
      LOG_LEVEL: "INFO"

    # No command needed; entrypoint auto-injects flags based on env
```

Start it up:
```bash
docker compose up -d
docker logs -f pyrmqtt
```

Expected logs:
```
INFO pyrmqtt Connected to RAVEn device=/serial
INFO PVOutput uploader running: interval=300s, net=0, topic=raven/sensor/telemetry  # only if enabled
INFO Connected to MQTT; subscribing raven/sensor/telemetry
```

---

## MQTT data model

Default topic: `raven/sensor/telemetry`  
Typical JSON fields:
- `raw_demand` — instantaneous power in **W** (positive=import, negative=export)
- `demand` — instantaneous power in **kW** (fallback if `raw_demand` not present)
- `summation_delivered` — lifetime import (kWh)
- `summation_received` — lifetime export (kWh)

> The PVOutput worker listens to `PVOUTPUT_MQTT_TOPIC` (default: `raven/sensor/telemetry`).

---

## PVOutput behavior

- **Gross mode** (`PVOUTPUT_NET=0`, default):  
  Sends **import power** as `v4`. If you’re exporting (negative demand), it sends `v4=0` (matching the original shell script).

- **Net mode** (`PVOUTPUT_NET=1`):  
  Sends **export power** as `v2`, **import power** as `v4`, and `n=1` (net data).

- **Voltage** is posted as `v6` when available (skipped if Fronius is offline/asleep).

- Interval aligned posting; default 300s keeps within PVOutput rate limits.

---

## Environment variables

| Variable | Required | Default | Description |
|---|:--:|---:|---|
| `MQTT_HOST` | ✅ | — | MQTT hostname/IP (no scheme, no `:port`) |
| `MQTT_PORT` | ✅ | `1883` | MQTT port |
| `MQTT_USERNAME` | ❓ | — | MQTT username if broker requires auth |
| `MQTT_PASSWORD` | ❓ | — | MQTT password |
| `RAVEN_DEVICE` | ✅ | `/serial` | Serial device inside container |
| `PVOUTPUT_ENABLED` | ❓ | `false` | Enable PVOutput worker (`true`/`false`) |
| `PVOUTPUT_API_KEY` | ⚠️ | — | Required if PVOutput is enabled |
| `PVOUTPUT_SYSTEM_ID` | ⚠️ | — | Required if PVOutput is enabled |
| `PVOUTPUT_NET` | ❓ | `0` | `0`=gross, `1`=net (v2/v4 with `n=1`) |
| `PVOUTPUT_INTERVAL_SECONDS` | ❓ | `300` | Upload cadence in seconds |
| `PVOUTPUT_MQTT_TOPIC` | ❓ | `raven/sensor/telemetry` | Source topic for PVOutput worker |
| `FRONIUS_HOST` | ❓ | — | Fronius IP/host for voltage (optional) |
| `FRONIUS_DEVICE_ID` | ❓ | `1` | Device ID |
| `FRONIUS_USERNAME` | ❓ | — | Basic auth user (rare) |
| `FRONIUS_PASSWORD` | ❓ | — | Basic auth pass |
| `FRONIUS_TIMEOUT` | ❓ | `3.0` | HTTP timeout (seconds) |
| `TZ` | ✅ | `Australia/Melbourne` | Timezone for PVOutput `d`/`t` |
| `LOG_LEVEL` | ❓ | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

> **Security:** Prefer Docker secrets or a `.env` file (not committed) for credentials and API keys.

---

## Home Assistant example

Instantaneous **import** sensor (0 when exporting):
```yaml
sensor Meter:
  - platform: mqtt
    state_topic: "raven/sensor/telemetry"
    name: "Current Demand"
    unit_of_measurement: 'kW'
    icon: mdi:flash
    value_template: >-
      {% if "demand" in value_json %}
        {{ value_json.demand }}
      {% else %}
        {{ states('sensor.current_demand') }}
      {% endif %}
  - platform: mqtt
    state_topic: "raven/sensor/telemetry"
    name: "Total Import"
    unit_of_measurement: 'kWh'
    icon: mdi:flash
    value_template: >-
      {% if "summation_delivered" in value_json %}
        {{ value_json.summation_delivered }}
      {% else %}
        {{ states('sensor.total_import') }}
      {% endif %}
  - platform: mqtt
    state_topic: "raven/sensor/telemetry"
    name: "Total Export"
    unit_of_measurement: 'kWh'
    icon: mdi:flash
    value_template: >-
      {% if "summation_received" in value_json %}
        {{ value_json.summation_received }}
      {% else %}
        {{ states('sensor.total_export') }}
      {% endif %}
```
Instantaneous **import** sensor (Will show negative values):
```yaml
    - name: "Current Demand"
      state_topic: "raven/sensor/telemetry"
      state_class: "measurement"
      device_class: "energy"
      unit_of_measurement: "kWh"
      icon: mdi:flash
      value_template: >-
        {% if "demand" in value_json %}
        {{ 0 if value_json.demand <=0 else value_json.demand }}
        {% endif %}
    - name: "Current Export"
      state_topic: "raven/sensor/telemetry"
      state_class: "measurement"
      device_class: "energy"
      unit_of_measurement: "kWh"
      icon: mdi:flash
      value_template: >-
        {% if "demand" in value_json %}
        {{ 0 if value_json.demand > 0 else value_json.demand|abs  }}
        {% endif %}
    - name: "Total Import"
      state_topic: "raven/sensor/telemetry"
      state_class: "measurement"
      device_class: "energy"
      unit_of_measurement: "kWh"
      icon: mdi:flash
      value_template: >-
        {% if "summation_delivered" in value_json %}
          {{ value_json.summation_delivered }}
        {% else %}
          {{ states('sensor.total_import') }}
        {% endif %}
    - name: "Total Export"
      state_topic: "raven/sensor/telemetry"
      state_class: "measurement"
      device_class: "energy"
      unit_of_measurement: "kWh"
      icon: mdi:flash
      value_template: >-
        {% if "summation_received" in value_json %}
          {{ value_json.summation_received }}
        {% else %}
          {{ states('sensor.total_export') }}
        {% endif %}
```

---

## Building locally

```bash
# From repo root
docker build -t your/pyraven-mqtt:latest .
```

The Dockerfile is multi-stage (`testing` ➜ `pylint` ➜ `runtime`).  
If you need a lenient build (skip style warnings as fatal), build with:
```bash
docker build --build-arg PYLINT_STRICT=false -t your/pyraven-mqtt:latest .
```

---

## Troubleshooting

- **Fronius at night**: The inverter sleeps; voltage fetch returns `None` and is skipped. The worker continues posting power data normally.
- **MQTT rc=5 (Not authorized)**: Set `MQTT_USERNAME`/`MQTT_PASSWORD` and ensure ACLs allow publish to `raven/#`.
- **Invalid host**: Use plain `MQTT_HOST` (e.g., `192.168.1.10`), keep port in `MQTT_PORT`.
- **Serial mapping**: On the host, list stable paths:
  ```bash
  ls -l /dev/serial/by-id/
  ```
  Map the RAVEn entry to `/serial` in your compose file.

- **Rate limits**: If you see PVOutput rate limiting, increase `PVOUTPUT_INTERVAL_SECONDS` (e.g., `600` for 10 minutes).

---

## Development

- Python 3.9+
- Dependencies managed via Pipenv for dev, installed system-wide in the image
- Lint: `pylint *.py`

Key files:
- `pyrmqtt.py` — RAVEn ➜ MQTT publisher
- `pvoutput_uploader.py` — optional PVOutput worker
- `entry.sh` — entrypoint; injects CLI flags for `pyrmqtt.py` from env and launches the worker if enabled

---

## Changelog (high level)

- **vNext**
  - Optional PVOutput uploader (Python)
  - Optional Fronius voltage integration
  - Env-driven configuration (no cron/jq/curl)
  - Docker entrypoint auto-wires flags from env
  - Updated documentation

---

## License

See `LICENSE` in this repository.
