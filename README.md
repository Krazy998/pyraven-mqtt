# RFA RAVEn -> MQTT

## Build

```shell
export DOCKER_BUILDKIT=1
docker build -t pyrmqtt .
```

## Run

```shell
docker run \
    --detach \
    --tty \
    --restart=always \
    --name=pyrmqtt \
    --username=username \
    --password=password \
    --device="$(readlink -f /dev/serial/by-id/usb-Rainforest_RFA-Z106-RA-PC_RAVEn_v2.3.21-if00-port0):/serial" \
    pyrmqtt \
        --host "${MQTT_BROKER}" \
        --device /serial
```
## Docker-compose

Please see docker-compose.yaml


## Ingest into Home Assistant (If negative values dont matter)

Add a sensor to `configuration.yaml`:

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
## Ingest into Home Assistant (If negative values matter)

Add a sensor to `configuration.yaml`:

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
