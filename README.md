# octo2influx

Import Octopus Energy electricity, export, gas, and tariff data into InfluxDB 3
and analyse it in Grafana.

octo2influx provides:

- incremental, replay-safe ingestion with an explicit checkpoint per stream;
- optional meter and tariff discovery from an Octopus account;
- single-rate, Economy 7 day/night, and configurable multi-rate tariffs;
- raw usage and tariff measurements that remain backward compatible;
- materialised usage costs and one standing charge per supply per local day;
- isolation between streams, with failed streams retried without blocking others;
- a provisioned Grafana SQL datasource, two dashboards, and ingestion health.

## Docker Compose quick start

The example stack pins InfluxDB 3 Core and Grafana, binds their ports to
`127.0.0.1`, disables anonymous Grafana access, and provisions both dashboards.

1. Create local files:

   ```shell
   cp docker-compose.example.yml docker-compose.yml
   cp src/config.example.yaml config.yaml
   cp .env.example .env
   ```

2. Start InfluxDB and create its administrator token:

   ```shell
   docker compose up -d influx
   docker compose exec influx influxdb3 create token --admin
   ```

3. Put the token in both locations:

   - `config.yaml` as `influx_api_token`;
   - `.env` as `INFLUXDB_TOKEN`, for the provisioned Grafana datasource.

   InfluxDB 3 Core tokens are administrator tokens. Keep both files private.

4. Create the database:

   ```shell
   docker compose exec influx influxdb3 create database octo2influx --token YOUR_TOKEN
   ```

5. Configure Octopus data in `config.yaml` using either account discovery or
   explicit meter/tariff entries, then start the stack:

   ```shell
   docker compose up -d
   docker compose logs -f octo2influx
   ```

Grafana is available at <http://localhost:3000>. Sign in with Grafana's default
administrator credentials and change the password when prompted. The InfluxDB
SQL datasource and dashboard are provisioned automatically.

The importer runs hourly (`FREQ`) and retries failed runs after five minutes
(`RETRY_FREQ`). Container health becomes healthy only after a successful sync.

## Configure Octopus data

Copy [`src/config.example.yaml`](src/config.example.yaml) and never commit the
result: it contains account identifiers and API tokens.

### Account discovery

Set an Octopus account number and remove unused placeholder entries:

```yaml
account_number: "A-XXXXXXXX"
usage: []
tariffs: []
```

The authenticated account endpoint discovers active MPANs/MPRNs, meter serials,
import/export direction, and tariff agreements. Product metadata and applicable
rate endpoints are resolved without sending the account API key to public tariff
endpoints.

Explicit `usage` and `tariffs` entries can be mixed with discovery. Explicit
entries take precedence; discovery fills missing streams. Set
`discover_historical_tariffs: true` to include previous agreements, including
non-contiguous periods using the same tariff code.

Octopus does not expose whether a discovered gas meter returns `m3` or `kWh`, so
set `discovered_gas_unit` correctly.

### Explicit configuration

Without `account_number`, keep one entry per meter:

```yaml
usage:
  - energy_type: electricity
    direction: import
    meter_point: "YOUR_MPAN"
    meter_serial: "YOUR_SERIAL"
    unit: kWh
```

Tariffs can optionally define agreement bounds, payment method, endpoint types,
and whether their rates should be used to materialise comparison costs:

```yaml
tariffs:
  - energy_type: electricity
    direction: import
    product_code: "YOUR-PRODUCT"
    tariff_code: "E-2R-YOUR-PRODUCT-C"
    full_name: "Example Economy 7"
    display_name: "Example Economy 7"
    description: ""
    materialize_costs: false
    rate_types:
      - day-unit-rates
      - night-unit-rates
      - standing-charges
```

`materialize_costs: false` keeps the raw tariff history available in Grafana
without creating usage or standing-charge comparisons for that tariff. This is
useful when a tariff exposes multiple payment-method prices and no single method
should be assumed.

### Multi-rate schedules

Dual- and multi-rate endpoints provide their prices but not a universal schedule
for deciding which register applies to each interval. Configure the tariff's
local-time schedule before costs are materialised:

```yaml
tariff_schedules:
  "E-2R-YOUR-PRODUCT-C":
    timezone: Europe/London
    default_price_type: day-unit-rates
    periods:
      - price_type: night-unit-rates
        start: "00:30"
        end: "07:30"
```

Overnight periods may cross midnight. Rates are still imported when a schedule
is absent, but cost materialisation is skipped with a clear warning.

### Gas costs

Some gas meters report `kWh`; others report `m3`. Raw data retains its original
unit and Grafana can display either. Gas tariffs are priced in kWh, so estimating
cost for an `m3` stream requires a conversion factor from the bill:

```yaml
gas_m3_to_kwh_factor: 11.1868
```

Calorific value changes over time. This estimate is useful for monitoring, not a
replacement for the supplier's bill.

## Data model

The default database contains:

| Measurement | Purpose |
|---|---|
| `octopus-usage` | Raw consumption/export. Legacy unit-named fields remain; `value` and `unit` provide a normalized representation. |
| `octopus-tariffs` | Raw tariff rates, normalized values, units, and source validity metadata. |
| `octopus-costs` | Materialised usage cost/revenue and daily standing charges for each compatible tariff comparison. |
| `octopus-watermarks` | Explicit source-coverage checkpoint for every raw and derived stream. |
| `octopus-sync-status` | Latest run status, duration, and successful/failed stream counts. |

Usage costs are recorded at the source interval midpoint. Standing charges are
recorded once at local midnight per MPAN/MPRN and tariff, not once per meter
serial or half-hour. This remains correct on 23- and 25-hour daylight-saving
days and across meter replacements.

InfluxDB deduplicates identical timestamp/tag points, so replaying a page after a
failure is safe. A checkpoint is committed only with the final write of a
successful page. If one meter, tariff, or derived-cost stream fails, independent
streams continue; the process exits non-zero after recording a failure summary.

## Backfills and migration

By default, new streams look back `from_max_days_ago` days and then resume from
their checkpoints. Force a historical range with:

```shell
python3 src/octo2influx.py --from_days_ago 365 --to_days_ago 0
```

`from_days_ago` must be greater than or equal to `to_days_ago`.

Existing raw measurements remain compatible. On the first checkpoint-enabled
run, legacy usage timestamps seed consumption progress; tariffs replay the
bounded lookback because older versions generated future boundary points that
are not safe checkpoints. The replay is idempotent and creates normalized
columns plus derived costs. Use an explicit backfill to materialise costs beyond
the normal lookback.

Octopus normally publishes smart-meter consumption the following day.

## Run directly with Python

Python 3.10 or newer is supported:

```shell
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r src/requirements.txt
cp src/config.example.yaml config.yaml
python3 src/octo2influx.py
```

On Windows, activate with `.venv\Scripts\Activate.ps1`. Create the InfluxDB
database and token first, and use `http://localhost:8181` as `influx_url`.

Configuration priority is environment, command line, then configuration file.
Secrets are rejected on the command line. Run
`python3 src/octo2influx.py --help` for runtime settings.

## Grafana

Docker Compose provisions:

- an InfluxDB 3 SQL datasource using `INFLUXDB_TOKEN` from `.env`;
- [`grafana/dashboard.json`](grafana/dashboard.json), an operational overview
  inspired by modern home-energy dashboards, with six colored KPI tiles, daily
  grid energy, cost/revenue, rate trends, hour-of-day usage, cumulative energy,
  gas, and ingestion health;
- [`grafana/historical-dashboard.json`](grafana/historical-dashboard.json), a
  recent analysis view with tariff timelines, per-meter history, tariff
  comparison, and cumulative totals;
- variables for datasource, measurements, gas unit, account timezone, and
  comparison tariffs;
- summed multi-meter usage, materialised tariff costs, DST-safe daily totals,
  multi-rate price series, and latest synchronization health.

Solar generation, battery state/power, and account balance are not shown because
the importer does not collect those sources. The layout deliberately avoids
inventing values to fill attractive rectangles.

InfluxDB 3 Core does not compact Parquet files. The supplied Core stack therefore
uses a bounded query-file limit with a three-day overview and seven-day analysis
default. Longer raw-data ranges require InfluxDB 3 Enterprise compaction or
separately maintained aggregate measurements.

For an external Grafana installation, import either dashboard and select an
InfluxDB datasource configured with SQL, the target database, token, and
`insecureGrpc: true` for a plain HTTP endpoint.

The JSON files are generated deterministically from
[`grafana/generate_dashboards.py`](grafana/generate_dashboards.py):

```shell
python3 grafana/generate_dashboards.py
python3 grafana/generate_dashboards.py --check
```

## Reverse proxy

The Compose example includes commented Traefik labels. Attach the selected
services to the external `proxy` network, replace the hostnames and certificate
resolver, set `GF_SERVER_ROOT_URL`, and remove local port mappings if access
should be proxy-only. Do not expose InfluxDB unless it is required.

## Development

```shell
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q -m "not integration"
```

CI tests Python 3.10 and 3.13, validates Compose, builds the image, boots real
InfluxDB and Grafana instances, verifies both provisioned dashboards and their
datasource, executes every panel SQL query, and proves idempotent sync and cost
calculation. Dependabot checks Python, Docker, and Actions dependencies monthly.

## Acknowledgements

This project is based on
[`stevenewey/octograph`](https://github.com/stevenewey/octograph) and
[`yo8192/octo2influx`](https://github.com/yo8192/octo2influx).
