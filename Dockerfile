FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RAVEN_DEVICE=/serial

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
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

# Ensure lint & tests run when building with BuildKit
COPY --from=test /app/pyrmqtt.py /app/pvoutput_uploader.py /app/
COPY entry.sh /app/entry.sh
RUN chmod +x /app/entry.sh

LABEL org.opencontainers.image.source="https://github.com/Krazy998/pyraven-mqtt" \
      org.opencontainers.image.description="Rainforest RAVEn USB to MQTT bridge with optional PVOutput uploader" \
      org.opencontainers.image.licenses="Unlicense"

ENTRYPOINT ["/app/entry.sh"]
