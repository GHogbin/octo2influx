# InfluxDB Enterprise cutover cleanup

This runbook safely retires the original InfluxDB 3 Core deployment after
`octo2influx` has been cut over to InfluxDB 3 Enterprise At-Home.

Do not delete the Core volume immediately. Keep it for at least seven successful
days after cutover so rollback does not depend on restoring an archive.

## Deployment assets

| Asset | Purpose | Cleanup status |
|---|---|---|
| `octo2influx_influxdb3enterprise` | Active Enterprise data and licence | Keep |
| `octo2influx_influxdb3data` | Original Core rollback volume | Keep for seven days, then archive and remove |
| `docker-compose.yml` | Base Compose configuration | Keep |
| `docker-compose.enterprise.yml` | Active Enterprise override | Keep |
| `.env` entry `COMPOSE_FILE=docker-compose.yml:docker-compose.enterprise.yml` | Activates the Enterprise override | Keep |
| `octo2influx-enterprise-test` | Temporary validation container | Remove after cutover |
| `/srv/docker-data/backups/octo2influx` | Deployment and volume backups | Keep |

Never run `docker volume prune`, `docker system prune --volumes`, or a forced
volume deletion as part of this procedure.

## 1. Verify the active Enterprise deployment

Run on the Docker host:

```bash
(
  set -euo pipefail

  cd /srv/docker-data/stacks/octo2influx
  set -a
  source .env
  set +a

  docker compose ps

  INFLUX_ID=$(docker compose ps -q influx)
  docker inspect "$INFLUX_ID" \
    --format 'Image={{.Config.Image}}{{range .Mounts}}{{println}}{{.Name}} -> {{.Destination}}{{end}}'

  docker compose exec -T influx \
    influxdb3 query \
    --database octo2influx \
    --token "$INFLUXDB_TOKEN" \
    "SELECT COUNT(*) AS rows, MIN(time), MAX(time)
     FROM \"octopus-usage\"
     WHERE time >= now() - INTERVAL '30 days'"
)
```

Expected values include:

```text
Image=influxdb:3.11.2-enterprise
octo2influx_influxdb3enterprise -> /var/lib/influxdb3
```

Confirm the importer has completed a recent synchronization:

```bash
cd /srv/docker-data/stacks/octo2influx

docker compose logs --since 24h octo2influx 2>&1 |
  grep -E 'Synchronization (completed|failed)|Sleeping'
```

The latest outcome must be:

```text
Synchronization completed.
Sleeping 1h...
```

The subshell prevents values sourced from the Enterprise `.env` file from
persisting in the interactive parent shell.

## 2. Remove the temporary test container

This does not remove its Enterprise volume:

```bash
cd /srv/docker-data/stacks/octo2influx

if docker container inspect \
    octo2influx-enterprise-test >/dev/null 2>&1; then
  docker stop octo2influx-enterprise-test
  docker rm octo2influx-enterprise-test
fi
```

Confirm only the Compose-managed InfluxDB container remains:

```bash
docker ps -a \
  --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' |
  grep -E 'NAMES|octo2influx.*influx'
```

## 3. Complete the seven-day observation period

During the observation period, confirm:

1. All three Compose services remain running.
2. InfluxDB and the importer remain healthy.
3. Hourly synchronizations continue to complete.
4. The 30-day Overview renders successfully.
5. InfluxDB is not restarting or being OOM-killed.

Check service state:

```bash
cd /srv/docker-data/stacks/octo2influx

docker compose ps

for SERVICE in influx octo2influx; do
  ID=$(docker compose ps -q "$SERVICE")
  docker inspect "$ID" \
    --format "$SERVICE status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}} oom={{.State.OOMKilled}}"
done
```

Do not continue if a service is unhealthy, restarting, or producing failed
synchronizations.

## 4. Confirm the Core volume is detached

Set the volume names:

```bash
OLD_VOL=octo2influx_influxdb3data
ACTIVE_VOL=octo2influx_influxdb3enterprise
```

Confirm both volumes exist:

```bash
docker volume inspect "$OLD_VOL" "$ACTIVE_VOL" >/dev/null
```

Fail if any container still references the Core volume:

```bash
REFERENCES=$(docker ps -aq --filter "volume=$OLD_VOL")

if [ -n "$REFERENCES" ]; then
  echo "Core volume is still referenced by:"
  docker ps -a \
    --filter "volume=$OLD_VOL" \
    --format 'table {{.ID}}\t{{.Names}}\t{{.Status}}'
  exit 1
fi

echo "Core volume is detached."
```

## 5. Create and verify the final Core archive

The Core volume is detached, so it can be archived without stopping Enterprise.
Set `CUTOVER_BACKUP` to the pre-cutover backup created before Enterprise was
enabled:

```bash
CUTOVER_BACKUP=/srv/docker-data/backups/octo2influx/cutover-YYYYMMDDTHHMMSSZ
```

Create the final archive and include both active and pre-cutover configuration.
The subshell aborts on the first failed command without changing the parent
shell:

```bash
(
  set -euo pipefail

  OLD_VOL=octo2influx_influxdb3data
  STAMP=$(date -u +%Y%m%dT%H%M%SZ)
  BACKUP=/srv/docker-data/backups/octo2influx/core-final-$STAMP

  docker volume inspect "$OLD_VOL" >/dev/null

  for FILE in docker-compose.yml .env config.yaml; do
    test -r "$CUTOVER_BACKUP/$FILE"
  done

  for FILE in \
      docker-compose.yml \
      docker-compose.enterprise.yml \
      .env \
      config.yaml; do
    test -r "/srv/docker-data/stacks/octo2influx/$FILE"
  done

  mkdir -p "$BACKUP/active" "$BACKUP/pre-cutover"
  chmod 700 "$BACKUP"

  cp -a \
    /srv/docker-data/stacks/octo2influx/docker-compose.yml \
    /srv/docker-data/stacks/octo2influx/docker-compose.enterprise.yml \
    /srv/docker-data/stacks/octo2influx/.env \
    /srv/docker-data/stacks/octo2influx/config.yaml \
    "$BACKUP/active/"

  cp -a \
    "$CUTOVER_BACKUP/docker-compose.yml" \
    "$CUTOVER_BACKUP/.env" \
    "$CUTOVER_BACKUP/config.yaml" \
    "$BACKUP/pre-cutover/"

  test ! -e "$BACKUP/core-volume.tar"

  docker run --rm \
    -v "$OLD_VOL:/source:ro" \
    -v "$BACKUP:/backup" \
    ubuntu:24.04 \
    bash -c 'cd /source && tar -cpf /backup/core-volume.tar .'

  test -s "$BACKUP/core-volume.tar"
  tar -tf "$BACKUP/core-volume.tar" >/dev/null

  (
    cd "$BACKUP"
    sha256sum \
      core-volume.tar \
      active/docker-compose.yml \
      active/docker-compose.enterprise.yml \
      active/.env \
      active/config.yaml \
      pre-cutover/docker-compose.yml \
      pre-cutover/.env \
      pre-cutover/config.yaml \
      > SHA256SUMS
    sha256sum -c SHA256SUMS
  )

  printf 'Verified backup: %s\n' "$BACKUP"
)
```

Set `BACKUP` to the verified path printed by the script, then inspect a short
listing:

```bash
BACKUP=/srv/docker-data/backups/octo2influx/core-final-YYYYMMDDTHHMMSSZ

cd /srv/docker-data/stacks/octo2influx

tar -tf "$BACKUP/core-volume.tar" >/dev/null
tar -tf "$BACKUP/core-volume.tar" | sed -n '1,20p'
du -h "$BACKUP/core-volume.tar"
```

Every checksum must report `OK`, the complete unpiped archive check must succeed,
and the listing must contain InfluxDB data files.

## 6. Remove only the old Core volume

Repeat all deletion gates in one fail-closed subshell:

```bash
(
  set -euo pipefail

  OLD_VOL=octo2influx_influxdb3data
  ACTIVE_VOL=octo2influx_influxdb3enterprise
  BACKUP=/srv/docker-data/backups/octo2influx/core-final-YYYYMMDDTHHMMSSZ

  docker volume inspect "$OLD_VOL" >/dev/null
  docker volume inspect "$ACTIVE_VOL" >/dev/null

  REFERENCES=$(docker ps -aq --filter "volume=$OLD_VOL")
  test -z "$REFERENCES"

  (
    cd "$BACKUP"
    sha256sum -c SHA256SUMS
  )
  tar -tf "$BACKUP/core-volume.tar" >/dev/null

  docker volume inspect "$OLD_VOL"
  docker volume rm "$OLD_VOL"

  docker volume inspect "$ACTIVE_VOL" >/dev/null
  docker compose ps
)
```

The command deliberately omits `--force`. Docker also refuses removal if a
container still references the volume.

## 7. Optionally remove the Core image

List locally cached InfluxDB images:

```bash
docker image ls influxdb \
  --format 'table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}'
```

After the Core volume archive is verified and Core rollback is no longer
required, remove the exact unused image without forcing it:

```bash
docker image rm influxdb:3.11.2-core
```

Docker will refuse removal if a container still uses the image.

## Roll back before deleting the Core volume

Set the path to the pre-cutover backup:

```bash
BACKUP=/srv/docker-data/backups/octo2influx/cutover-YYYYMMDDTHHMMSSZ
```

Stop Enterprise and restore the original deployment files:

```bash
(
  set -euo pipefail

  cd /srv/docker-data/stacks/octo2influx

  for FILE in docker-compose.yml .env config.yaml; do
    test -r "$BACKUP/$FILE"
  done
  if grep -Eq \
      '^[[:space:]]*(export[[:space:]]+)?COMPOSE_FILE[[:space:]]*=' \
      "$BACKUP/.env"; then
    echo "Backup .env is not from the pre-Enterprise deployment."
    exit 1
  else
    GREP_STATUS=$?
    test "$GREP_STATUS" -eq 1
  fi

  docker compose stop -t 60 octo2influx grafana influx

  cp "$BACKUP/docker-compose.yml" docker-compose.yml
  cp "$BACKUP/.env" .env
  cp "$BACKUP/config.yaml" config.yaml
  chmod 600 .env
  chown root:9923 config.yaml
  chmod 640 config.yaml

  unset \
    COMPOSE_FILE \
    INFLUXDB3_LICENSE_EMAIL \
    INFLUXDB3_LICENSE_TYPE

  set -a
  source .env
  set +a
  unset \
    COMPOSE_FILE \
    INFLUXDB3_LICENSE_EMAIL \
    INFLUXDB3_LICENSE_TYPE

  docker compose \
    --env-file .env \
    -f docker-compose.yml \
    config --format json > /tmp/octo2influx-core-config.json

  python3 - <<'PY'
import json

with open('/tmp/octo2influx-core-config.json', encoding='utf-8') as source:
    config = json.load(source)

influx = config['services']['influx']
assert influx['image'].endswith('-core'), influx['image']
assert any(
    volume.get('source') == 'influxdb3data'
    and volume.get('target') == '/var/lib/influxdb3'
    for volume in influx['volumes']
), influx['volumes']
print('Core Compose configuration verified.')
PY

  docker compose \
    --env-file .env \
    -f docker-compose.yml \
    up -d
  docker compose \
    --env-file .env \
    -f docker-compose.yml \
    ps

  rm -f /tmp/octo2influx-core-config.json
)

unset \
  COMPOSE_FILE \
  INFLUXDB3_LICENSE_EMAIL \
  INFLUXDB3_LICENSE_TYPE
```

Do not point Core at the Enterprise volume. The Enterprise-upgraded catalog is
not Core-compatible. The final `unset` runs in the parent shell so a later plain
`docker compose` command cannot reactivate the Enterprise override.

## Stage Core recovery from the final archive

Use this only after the original Core volume has been removed. Recovery is
staged into a new, randomly named volume. It does not modify the active
Enterprise deployment, start Core, or automatically delete anything on failure.

```bash
ARCHIVE=/srv/docker-data/backups/octo2influx/core-final-YYYYMMDDTHHMMSSZ

(
  set -euo pipefail

  (
    cd "$ARCHIVE"
    sha256sum -c SHA256SUMS
  )
  tar -tf "$ARCHIVE/core-volume.tar" >/dev/null

  RESTORE_ID=$(tr -d '-' < /proc/sys/kernel/random/uuid)
  RESTORED_VOL="octo2influx_core_restore_$RESTORE_ID"
  RESTORE_MARKER="octo2influx-core-restore-$RESTORE_ID"

  docker volume create \
    --label "octo2influx.restore-marker=$RESTORE_MARKER" \
    "$RESTORED_VOL"

  VOLUME_MARKER=$(docker volume inspect \
    --format '{{index .Labels "octo2influx.restore-marker"}}' \
    "$RESTORED_VOL")
  test "$VOLUME_MARKER" = "$RESTORE_MARKER"

  docker run --rm \
    -v "$RESTORED_VOL:/target" \
    ubuntu:24.04 \
    bash -c 'test -z "$(find /target -mindepth 1 -maxdepth 1 -print -quit)"'

  docker run --rm \
    -v "$RESTORED_VOL:/target" \
    -v "$ARCHIVE:/backup:ro" \
    ubuntu:24.04 \
    bash -c 'cd /target && tar -xpf /backup/core-volume.tar'

  docker run --rm \
    -v "$RESTORED_VOL:/target:ro" \
    ubuntu:24.04 \
    bash -c 'test -n "$(find /target -mindepth 1 -maxdepth 1 -print -quit)"'

  printf 'Staged Core recovery volume: %s\n' "$RESTORED_VOL"
)
```

If staging fails, leave the uniquely named volume untouched for inspection.
Do not rerun the block and do not remove the volume by wildcard.

Before using a staged recovery, create and validate a separate Core Compose
override that references the exact printed volume name. Keep Enterprise stopped
but intact, and compare the restored database against the archive before any
rollback. Data written only after the Enterprise cutover will not exist in the
Core archive.
