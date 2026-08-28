#!/bin/bash

set -u

FREQ="${FREQ:-1h}"
RETRY_FREQ="${RETRY_FREQ:-5m}"
HEALTH_FILE="${HEALTH_FILE:-/tmp/octo2influx-health}"
child_pid=""

usage() {
    echo "FREQ={positive seconds or s, m, h, d suffix} RETRY_FREQ={same} $0"
    echo "  FREQ: delay after a successful synchronization."
    echo "  RETRY_FREQ: delay after a failed synchronization."
    exit 1
}

validate_duration() {
    local name="$1"
    local value="$2"
    if ! [[ "$value" =~ ^[1-9][0-9]*[smhd]?$ ]]; then
        echo "Invalid $name '$value'"
        usage
    fi
}

timestamp() {
    date -Iseconds
}

terminate() {
    echo "$(timestamp) Stopping..."
    if [[ -n "$child_pid" ]]; then
        kill -TERM "$child_pid" 2>/dev/null || true
        wait "$child_pid" 2>/dev/null || true
    fi
    exit 0
}

wait_for() {
    sleep "$1" &
    child_pid=$!
    wait "$child_pid"
    child_pid=""
}

validate_duration "FREQ" "$FREQ"
validate_duration "RETRY_FREQ" "$RETRY_FREQ"
trap terminate TERM INT

echo "$(timestamp) Starting with FREQ=$FREQ and RETRY_FREQ=$RETRY_FREQ..."
printf 'starting\n' > "$HEALTH_FILE"
echo "$(timestamp) Waiting 7 seconds before the first synchronization..."
wait_for 7

while :; do
    echo "$(timestamp) Starting synchronization..."
    python3 ./octo2influx.py &
    child_pid=$!
    if wait "$child_pid"; then
        delay="$FREQ"
        printf 'success\n' > "$HEALTH_FILE"
        echo "$(timestamp) Synchronization completed."
    else
        status=$?
        delay="$RETRY_FREQ"
        printf 'failed\n' > "$HEALTH_FILE"
        echo "$(timestamp) Synchronization failed with exit code $status." >&2
    fi
    child_pid=""

    echo "$(timestamp) Sleeping $delay..."
    wait_for "$delay"
done
