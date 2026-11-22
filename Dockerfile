FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RAVEN_DEVICE=/serial

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

##----------------------------------------------------------------------

FROM base AS test

COPY requirements.txt requirements-dev.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt \
    && rm -f /tmp/requirements.txt /tmp/requirements-dev.txt

COPY .pylintrc ./
COPY pyrmqtt.py pvoutput_uploader.py ./
COPY tests ./tests

RUN pylint --rcfile=.pylintrc pyrmqtt.py pvoutput_uploader.py \
    && python -m pytest -v

##----------------------------------------------------------------------

FROM base AS runtime

RUN getent group dialout >/dev/null || groupadd --system dialout \
    && useradd --system --uid 10001 --gid dialout --no-create-home --shell /usr/sbin/nologin appuser

# Ensure lint & tests run when building with BuildKit
COPY --from=test --chown=root:root /app/pyrmqtt.py /app/pvoutput_uploader.py /app/
COPY --chown=root:root entry.sh /app/entry.sh
RUN chmod 0644 /app/pyrmqtt.py /app/pvoutput_uploader.py \
    && chmod 0755 /app/entry.sh \
    && (pip uninstall -y setuptools wheel pip || true) \
    && rm -rf /usr/local/lib/python*/ensurepip \
              /usr/local/lib/python*/site-packages/setuptools* \
              /usr/local/lib/python*/site-packages/wheel* \
              /usr/local/lib/python*/site-packages/pip* \
              /usr/local/bin/pip* \
    && python -c "import pyrmqtt, pvoutput_uploader, paho.mqtt.client, raven; from zoneinfo import ZoneInfo; ZoneInfo('Australia/Melbourne')"

LABEL org.opencontainers.image.source="https://github.com/Krazy998/pyraven-mqtt" \
      org.opencontainers.image.description="Rainforest RAVEn USB to MQTT bridge with optional PVOutput uploader" \
      org.opencontainers.image.licenses="Unlicense"

USER appuser

ENTRYPOINT ["/app/entry.sh"]
