# pyraven-mqtt

Publish real-time smart-meter telemetry from a **Rainforest RAVEn** Zigbee USB stick to **MQTT** (for Home Assistant or any MQTT consumer).  
Optionally, the same container can also **upload status to PVOutput** and fetch **AC voltage** from a **Fronius** inverter.

- Core service: `pyrmqtt.py` → RAVEn ➜ MQTT
- Optional worker: `pvoutput_uploader.py` → MQTT (+Fronius voltage) ➜ PVOutput

---

## Features

- Streams smart-meter data from RAVEn to MQTT (`<prefix>/sensor/telemetry`) with retained birth/LWT state (`<prefix>/state`)
- Automatic MQTT reconnect with exponential backoff, optional TLS, and graceful shutdown
- Optional PVOutput uploader on an interval-aligned schedule (default: 5 min) with staleness protection
- Optional Fronius AC voltage integration (CommonInverterData → `UAC`)
- Pre-built multi-arch Docker images (`linux/amd64`, `linux/arm64`) published to GitHub Container Registry on every release

---

## How it works (high level)

```text
RAVEn USB  ──>  pyrmqtt.py  ── MQTT publish ──>  Broker (Home Assistant, etc.)

                                                      │
                pvoutput_uploader.py (optional)  <────┘
                  ├─ subscribes to MQTT telemetry
                  ├─ queries Fronius voltage (optional)
                  └─ posts to PVOutput on interval
```

---

## Quick start (Docker Compose)

> **Tip:** Map your RAVEn by stable path (e.g., `/dev/serial/by-id/...`) to avoid `/dev/ttyUSB*` renumbering across reboots.

```yaml
services:
  pyrmqtt:
    image: ghcr.io/krazy998/pyraven-mqtt:latest
    container_name: pyrmqtt
    restart: always
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    group_add:
      - dialout

    # Map the RAVEn device into the container
    devices:
      - /dev/serial/by-id/usb-Rainforest_RFA-Z106-RA-PC_RAVEn_v2.3.21-if00-port0:/serial

    environment:
      # --- MQTT (required) ---
      MQTT_HOST: "192.168.1.10"
      MQTT_PORT: "1883"
      MQTT_USERNAME: "youruser"
      MQTT_PASSWORD: "yourpass"
      # MQTT_TOPIC: "raven"
      # MQTT_TLS: "false"

      # --- RAVEn device (optional; defaults to /serial in Docker) ---
      RAVEN_DEVICE: "/serial"

      # --- PVOutput uploader (optional) ---
      PVOUTPUT_ENABLED: "false"         # set "true" to enable
      PVOUTPUT_API_KEY: ""              # required if enabled
      PVOUTPUT_SYSTEM_ID: ""            # required if enabled
      PVOUTPUT_NET: "0"                 # 0=gross (v4 import only), 1=net (v2 export + v4 import)
      PVOUTPUT_INTERVAL_SECONDS: "300"  # 5 minutes
      PVOUTPUT_MQTT_TOPIC: "raven/sensor/telemetry"

      # --- Fronius voltage (optional) ---
      FRONIUS_HOST: "192.168.1.10"
      FRONIUS_DEVICE_ID: "1"
      # FRONIUS_USERNAME: ""
      # FRONIUS_PASSWORD: ""
      FRONIUS_TIMEOUT: "3.0"

      # --- Misc ---
      TZ: "Australia/Melbourne"
      LOG_LEVEL: "INFO"
```

Start it up:

```bash
docker compose up -d
docker logs -f pyrmqtt
```

Expected logs:

```text
INFO pyrmqtt Connected to RAVEn device=/serial
INFO pyrmqtt MQTT connected host=192.168.1.10 port=1883
INFO pvoutput PVOutput uploader running: interval=300s, net=0, max_age=600s, topic=raven/sensor/telemetry  # only if enabled
INFO pvoutput Connected to MQTT; subscribing raven/sensor/telemetry
```

---

## MQTT data model

Default topic prefix: `raven`

- `raven/state` — retained `"online"` / `"offline"` availability state (birth + LWT)
- `raven/sensor/telemetry` — JSON payloads with an added `ts` epoch timestamp

Typical JSON fields:
- `raw_demand` — instantaneous power in **W** (positive = import, negative = export)
- `demand` — instantaneous power in **kW** (fallback if `raw_demand` not present)
- `summation_delivered` — lifetime import (kWh)
- `summation_received` — lifetime export (kWh)

> The PVOutput worker listens to `PVOUTPUT_MQTT_TOPIC` (defaults to `<MQTT_TOPIC>/sensor/telemetry`).

---

## PVOutput behavior

- **Gross mode** (`PVOUTPUT_NET=0`, default):  
  Sends **import power** as `v4`. If you are exporting (negative demand), it sends `v4=0`.

- **Net mode** (`PVOUTPUT_NET=1`):  
  Sends **export power** as `v2`, **import power** as `v4`, and `n=1` (net data).

- **Staleness protection**:  
  If no demand telemetry has been received within `PVOUTPUT_MAX_AGE_SECONDS` (default: `max(2 * interval, 120)`), the upload is skipped rather than posting stale or fake zero readings.

- **Voltage** is posted as `v6` when available (skipped if Fronius is offline/asleep).

- Interval-aligned posting; default `300s` keeps within PVOutput rate limits.

---

## Environment variables

| Variable | Required | Default | Description |
|---|:--:|---:|---|
| `MQTT_HOST` | Yes | — | MQTT hostname/IP (no scheme, no `:port`) |
| `MQTT_PORT` | No | `1883` | MQTT port |
| `MQTT_USERNAME` | No | — | MQTT username if broker requires auth |
| `MQTT_PASSWORD` | No | — | MQTT password |
| `MQTT_TOPIC` | No | `raven` | MQTT topic prefix |
| `MQTT_QOS` | No | `1` | Telemetry publish QoS (`0`, `1`, or `2`) |
| `MQTT_RETAIN` | No | `false` | Retain telemetry messages |
| `MQTT_TLS` | No | `false` | Enable TLS for MQTT connections |
| `MQTT_CAFILE` | No | — | Custom CA certificate path for TLS |
| `MQTT_INSECURE` | No | `false` | Skip TLS certificate verification |
| `RAVEN_DEVICE` | No | `/serial` (Docker) / `/dev/ttyUSB0` | Serial device path |
| `RAVEN_POLL_INTERVAL` | No | `1.0` | Seconds between RAVEn polls |
| `RAVEN_MAX_SERIAL_ERRORS` | No | `10` | Exit after N consecutive serial errors (`0` disables) |
| `PVOUTPUT_ENABLED` | No | `false` | Enable PVOutput worker (`true`/`false`) |
| `PVOUTPUT_API_KEY` | If enabled | — | PVOutput API key |
| `PVOUTPUT_SYSTEM_ID` | If enabled | — | PVOutput system ID |
| `PVOUTPUT_NET` | No | `0` | `0` = gross, `1` = net (`v2`/`v4` with `n=1`) |
| `PVOUTPUT_INTERVAL_SECONDS` | No | `300` | Upload cadence in seconds |
| `PVOUTPUT_MAX_AGE_SECONDS` | No | `max(2*interval, 120)` | Skip upload if latest demand is older than this |
| `PVOUTPUT_MQTT_TOPIC` | No | `<MQTT_TOPIC>/sensor/telemetry` | Source topic for PVOutput worker |
| `FRONIUS_HOST` | No | — | Fronius IP/host for voltage (optional) |
| `FRONIUS_DEVICE_ID` | No | `1` | Fronius device ID |
| `FRONIUS_USERNAME` | No | — | Basic auth username (rare) |
| `FRONIUS_PASSWORD` | No | — | Basic auth password |
| `FRONIUS_TIMEOUT` | No | `3.0` | HTTP timeout in seconds |
| `TZ` | No | `Australia/Melbourne` | Timezone for PVOutput `d`/`t` |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

> **Security:** Prefer Docker secrets or a `.env` file (not committed) for credentials and API keys.

---

## Home Assistant example

Modern Home Assistant `configuration.yaml` MQTT sensors:

```yaml
mqtt:
  sensor:
    - name: "Grid Import Power"
      state_topic: "raven/sensor/telemetry"
      availability_topic: "raven/state"
      device_class: "power"
      state_class: "measurement"
      unit_of_measurement: "kW"
      icon: mdi:transmission-tower-import
      value_template: >-
        {% if "demand" in value_json %}
          {{ 0 if value_json.demand <= 0 else value_json.demand }}
        {% else %}
          {{ this.state }}
        {% endif %}

    - name: "Grid Export Power"
      state_topic: "raven/sensor/telemetry"
      availability_topic: "raven/state"
      device_class: "power"
      state_class: "measurement"
      unit_of_measurement: "kW"
      icon: mdi:transmission-tower-export
      value_template: >-
        {% if "demand" in value_json %}
          {{ 0 if value_json.demand > 0 else value_json.demand | abs }}
        {% else %}
          {{ this.state }}
        {% endif %}

    - name: "Total Import"
      state_topic: "raven/sensor/telemetry"
      availability_topic: "raven/state"
      device_class: "energy"
      state_class: "total_increasing"
      unit_of_measurement: "kWh"
      icon: mdi:flash
      value_template: >-
        {% if "summation_delivered" in value_json %}
          {{ value_json.summation_delivered }}
        {% else %}
          {{ this.state }}
        {% endif %}

    - name: "Total Export"
      state_topic: "raven/sensor/telemetry"
      availability_topic: "raven/state"
      device_class: "energy"
      state_class: "total_increasing"
      unit_of_measurement: "kWh"
      icon: mdi:flash
      value_template: >-
        {% if "summation_received" in value_json %}
          {{ value_json.summation_received }}
        {% else %}
          {{ this.state }}
        {% endif %}
```

---

## Building locally & Releases

### Local build

```bash
docker build -t pyraven-mqtt:local .
```

The Dockerfile is multi-stage (`base` ➜ `test` [pylint + pytest] ➜ `runtime`).

### Automated releases & security patching (GHCR)

Publishing a GitHub Release (e.g., tag `v2.0.0`) triggers [`.github/workflows/docker-release.yml`](.github/workflows/docker-release.yml), which builds and pushes multi-arch (`linux/amd64`, `linux/arm64`) images to:

```text
ghcr.io/krazy998/pyraven-mqtt:2.0.0
ghcr.io/krazy998/pyraven-mqtt:2.0
ghcr.io/krazy998/pyraven-mqtt:2
ghcr.io/krazy998/pyraven-mqtt:latest
```

Pre-releases are tagged with their version tag only and do not overwrite `:latest`.

- **Automated Dependabot patching**: [`.github/dependabot.yml`](.github/dependabot.yml) checks daily for updates to the pinned `python:3.12-slim` base image digest and Python dependencies (and weekly for GitHub Actions).
- Every PR runs `pylint`, `pytest`, a Docker build, and a **Trivy** container vulnerability scan (failing on fixable `HIGH`/`CRITICAL` CVEs).
- When a non-major Dependabot PR passes CI, [`.github/workflows/dependabot-auto-deploy.yml`](.github/workflows/dependabot-auto-deploy.yml) automatically squash-merges it and—if `Dockerfile` or `requirements.txt` changed—cuts a new patch release (e.g., `v2.0.1`) and pushes updated multi-arch images to GHCR with no manual intervention. Major version bumps remain open for manual review.
- **Container hardening**: The runtime container runs as unprivileged user `appuser` (UID `10001`, group `dialout`) and supports `read_only: true` and `cap_drop: [ALL]`. If your host serial device belongs to a group other than `dialout` (GID `20`), run `stat -c '%g' /dev/serial/by-id/...` and set that numeric GID in `group_add`.

---

## Troubleshooting

- **Fronius at night**: The inverter sleeps; voltage fetch returns `None` and is skipped. The worker continues posting power data normally.
- **MQTT rc=5 / Not authorized**: Set `MQTT_USERNAME`/`MQTT_PASSWORD` and ensure ACLs allow publish/subscribe on `raven/#`.
- **Invalid host**: Use plain `MQTT_HOST` (e.g., `192.168.1.10`), keep port in `MQTT_PORT`.
- **Serial mapping**: On the host, list stable paths:
  ```bash
  ls -l /dev/serial/by-id/
  ```
  Map the RAVEn entry to `/serial` in your compose file.
- **Rate limits**: If you see PVOutput rate limiting, increase `PVOUTPUT_INTERVAL_SECONDS` (e.g., `600` for 10 minutes).

---

## Development

- Python 3.12+
- Install dependencies: `pip install -r requirements-dev.txt`
- Lint: `pylint --rcfile=.pylintrc pyrmqtt.py pvoutput_uploader.py`
- Test: `pytest -v`

Key files:
- `pyrmqtt.py` — RAVEn ➜ MQTT publisher
- `pvoutput_uploader.py` — optional PVOutput worker
- `entry.sh` — container entrypoint and process supervisor

---

## License

See [LICENSE.md](LICENSE.md) in this repository.
