FROM ubuntu:24.04

LABEL org.opencontainers.image.title="octo2influx" \
      org.opencontainers.image.description="Octopus Energy to InfluxDB 3 importer" \
      org.opencontainers.image.source="https://github.com/GHogbin/octo2influx"

WORKDIR /usr/src/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip bash ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --uid 9923 --create-home --shell /usr/sbin/nologin octo2influx

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=octo2influx:octo2influx octo2influx.py loop.sh ./
COPY --chown=octo2influx:octo2influx octo2influx_core ./octo2influx_core

USER octo2influx

ENV FREQ=1h \
    RETRY_FREQ=5m \
    HEALTH_FILE=/tmp/octo2influx-health

HEALTHCHECK --interval=1m --timeout=5s --start-period=10m --retries=3 \
    CMD test "$(cat "$HEALTH_FILE" 2>/dev/null)" = "success" || exit 1

ENTRYPOINT ["bash", "loop.sh"]
