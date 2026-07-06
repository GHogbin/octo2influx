FROM ubuntu:24.04

LABEL maintainer="Thibault Godouet <yo8192@users.noreply.github.com>"

WORKDIR /usr/src/app

# Install Python 3 and bash (loop.sh) on Ubuntu; ca-certificates for HTTPS calls
# to the Octopus/InfluxDB APIs. Clean apt lists to keep the image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip bash ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu 24.04's system Python is PEP 668 "externally managed", so install into
# an isolated virtualenv and put it first on PATH (python3 in loop.sh uses it):
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# python Confuse requires the home to exist:
RUN useradd -u 9923 octo2influx --create-home

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY octo2influx.py loop.sh ./

USER octo2influx

ENV FREQ=1h
ENTRYPOINT [ "bash", "loop.sh" ]
