FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS base

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
    && chmod 0755 /app/entry.sh

LABEL org.opencontainers.image.source="https://github.com/Krazy998/pyraven-mqtt" \
      org.opencontainers.image.description="Rainforest RAVEn USB to MQTT bridge with optional PVOutput uploader" \
      org.opencontainers.image.licenses="Unlicense"

USER appuser

ENTRYPOINT ["/app/entry.sh"]
