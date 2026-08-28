# octo2influx

Download Octopus Energy electricity import, electricity export, gas usage, and
tariff data into InfluxDB 3 and visualise it in Grafana.

![Grafana dashboard overview](images/grafana-dashboard-overview.png)

The importer retrieves configured meters and tariffs from the
[Octopus API](https://developer.octopus.energy/docs/api/), writes them as
timestamped InfluxDB measurements, and supports incremental synchronisation or
explicit historical backfills. The dashboard can compare import costs and
export revenue across configured tariffs.

![Grafana Agile tariff example](images/grafana-example-agile.png)

## Docker Compose quick start

The example stack runs InfluxDB 3 Core, Grafana, and the importer. Its published
ports bind to `127.0.0.1` and Grafana anonymous access is disabled by default.

1. Create local configuration files:

   ```shell
   cp docker-compose.example.yml docker-compose.yml
   cp src/config.example.yaml config.yaml
   ```

2. Edit `config.yaml`:

   - replace all meter, tariff, and token placeholders;
   - set `influx_url` to `http://influx:8181`;
   - keep only the usage streams and tariffs you need.

3. Start InfluxDB and create an admin token:

   ```shell
   docker compose up -d influx
   docker compose exec influx influxdb3 create token --admin
   ```

4. Put the generated token in `config.yaml`, then create the database:

   ```shell
   docker compose exec influx influxdb3 create database octo2influx --token YOUR_TOKEN
   ```

   InfluxDB 3 Core requires the database to be created explicitly before the
   importer can query or write it.

5. Start the complete stack:

   ```shell
   docker compose up -d
   docker compose logs -f octo2influx
   ```

Grafana is available at <http://localhost:3000>. On first login, use Grafana's
default administrator credentials and change the password when prompted.

The Compose file pins tested InfluxDB and Grafana versions. Override
`INFLUXDB_TAG`, `GRAFANA_TAG`, `INFLUX_PORT`, or `GRAFANA_PORT` through a local
`.env` file when required.

## Run directly with Python

Python 3.10 or newer is supported:

```shell
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r src/requirements.txt
cp src/config.example.yaml config.yaml
python3 src/octo2influx.py
```

On Windows, activate the environment with
`.venv\Scripts\Activate.ps1` instead.

Create the InfluxDB 3 database and token before running the importer. For a
local InfluxDB instance, leave `influx_url` as `http://localhost:8181`.

## Configuration

Use [`src/config.example.yaml`](src/config.example.yaml) as the starting point.
Never commit `config.yaml`; it contains utility-account identifiers and API
tokens.

Configuration is loaded from:

1. environment variables such as `OCTO2INFLUX_TIMEZONE`;
2. command-line arguments;
3. `config.yaml` in the current directory, `/etc/octo2influx`, or
   `~/.config/octo2influx`;
4. a directory selected with `OCTO2INFLUXDIR`.

Secrets are deliberately rejected on the command line because shell history and
process listings can expose them. Put secrets in an access-restricted
configuration file or environment variables.

Run `python3 src/octo2influx.py --help` for every available setting.

### Incremental updates and backfills

By default, each stream resumes from its latest InfluxDB timestamp, bounded by
`from_max_days_ago`. To force a historical range:

```shell
python3 src/octo2influx.py --from_days_ago 365 --to_days_ago 0
```

`from_days_ago` must be greater than or equal to `to_days_ago`. Large writes are
split according to `influx_write_batch_size`.

HTTP requests have a bounded timeout and retry transient connection failures,
HTTP 429 responses, and HTTP 5xx responses. In Docker, a failed synchronisation
is retried after `RETRY_FREQ` rather than waiting for the normal `FREQ`.

> [!NOTE]
> Octopus typically publishes smart-meter usage the following day.

## Grafana dashboard

1. Add an **InfluxDB** data source in Grafana.
2. Select **SQL** as the query language.
3. Set the URL to `http://influx:8181` for the Compose stack, or the URL of your
   external InfluxDB 3 service.
4. Set the database to `octo2influx` and enter the InfluxDB token.
5. Disable TLS for plain HTTP; enable it only when the endpoint is served over
   HTTPS.
6. Import [`grafana/dashboard.json`](grafana/dashboard.json) and select the data
   source when Grafana prompts.

The dashboard has variables for measurement names, tariffs, history duration,
and account timezone. Daily panels use InfluxDB's wall-clock binning so daylight
saving changes follow the selected account timezone.

Cost and revenue panels query two days before the visible range before applying
`locf()` (last observation carried forward). This gives a fixed tariff rate
available at the beginning of an arbitrary dashboard window. Validate calculated
figures against a bill before relying on them for financial decisions.

## Reverse proxy

The Compose example contains commented Traefik labels for Grafana and InfluxDB.
To enable them:

1. Create the shared network with `docker network create proxy`.
2. Uncomment the relevant labels and `networks` blocks.
3. Replace the example hostnames, entrypoint, and certificate resolver.
4. Set `GF_SERVER_ROOT_URL` to Grafana's public HTTPS URL.
5. Remove local `ports` mappings if the services should only be reachable
   through Traefik.

Avoid publishing the InfluxDB API unless remote access is required. Tokens
protect the API; a smaller network attack surface is still preferable.

## Development

```shell
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```

GitHub Actions tests Python 3.10 and 3.13, validates the Compose model, and
builds the container. Dependabot checks Python, Docker, and Actions dependencies
monthly.

## Acknowledgements

This project is based on
[`stevenewey/octograph`](https://github.com/stevenewey/octograph) and the later
InfluxDB implementation from
[`yo8192/octo2influx`](https://github.com/yo8192/octo2influx). It now targets
InfluxDB 3 and Grafana SQL.
