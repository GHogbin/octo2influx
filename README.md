[//]: # (google-site-verification: bgTzhEZsRR1dfKKZZBBYCEAkwWiVAmkYZE4SKUYvx-I)  
# octo2influx

Download your Octopus Energy usage, import and export (if you have solar panels) tariff data into your [InfluxDB](https://www.influxdata.com/products/influxdb-overview/) 3 database, and display in [Grafana](https://grafana.com/).

## Referral

If you are interested in this and would like to join Octopus, why not use my [referral link](https://share.octopus.energy/amber-birch-257)? This will give you and me [£50 each](https://octopus.energy/help-and-faqs/articles/i-have-a-question-about-octopus-pound50-referrals/).

## About

octo2influx retrieves your usage data and the tariffs you configure from the [Octopus API](https://developer.octopus.energy/docs/api/). This can then be displayed with the advanced Grafana dashboard: ![screenshot of the Grafana dashboard](images/grafana-dashboard-overview.png)

It automatically calculates the cost based on the tariff, and you can switch between tariffs to compare costs. For instance, switching to the 'Agile' tariff on the same time period of the dashboard above gives us a different cost: ![screenshot of the electricty cost with a different tariff](images/grafana-example-agile.png)

## Installation

To run locally, get a recent Python 3 (e.g. v3.10) and pip, optionally setup a virtualenv, and run:
```shell
pip3 install -r src/requirements.txt
```

Alternatively you can build your own Docker image with the [Dockerfile](src/Dockerfile) (an `ubuntu:24.04` base with Python 3 in a virtualenv), or use Docker Compose based on the [example configuration](docker-compose.example.yml).

## Configuration and usage

First, create your own `config.yaml` file based on the [provided example](src/config.example.yaml) which explains (in comments) how to get the information you need.

### InfluxDB 3

octo2influx writes to an [InfluxDB 3](https://docs.influxdata.com/influxdb3/core/) database (e.g. InfluxDB 3 Core). Before the first run you need:

- an InfluxDB 3 instance reachable at `influx_url` (default `http://localhost:8181`);
- an admin token, created with `influxdb3 create token --admin`, put into `config.yaml` as `influx_api_token`;
- a database named after `influx_database` (auto-created on first write, or `influxdb3 create database octo2influx`).

The [Docker Compose example](docker-compose.example.yml) starts InfluxDB 3 Core, Grafana and octo2influx together.

### Reverse proxy (Traefik)

To expose Grafana through an existing [Traefik](https://traefik.io/) reverse proxy (with automatic TLS), the [Docker Compose example](docker-compose.example.yml) includes an opt-in Traefik section. To enable it:

1. Create the shared network once: `docker network create https`.
2. In `docker-compose.yml`, uncomment the `networks:` block at the bottom of the file and the `labels:`/`networks:` block on the `grafana` service (and on the `influx` service if you want the InfluxDB 3 API served over HTTPS too).
3. Replace `grafana.example.com` / `influx.example.com` in the `Host(...)` rules (and in `GF_SERVER_ROOT_URL`) with your own domains, and set the `entrypoints`/`certresolver` to match your Traefik configuration.
4. The Traefik routers use the `websecure` entrypoint with TLS, so both endpoints are served over **HTTPS**. When routing InfluxDB through Traefik, set `influx_url` in `config.yaml` to the HTTPS host (e.g. `https://influx.example.com`).
5. Optionally remove the local `ports:` mappings once Traefik fronts the services.

Once ready, you can simply run:

```shell
python3 ./octo2influx.py
```
> [!NOTE]
> Octopus typically makes your usage data available the next day.

The utility has flexible command line parameters and a nice help too:

```
python3 ./octo2influx.py --help
usage: octo2influx [-h] [--from_max_days_ago FROM_MAX_DAYS_AGO] [--from_days_ago FROM_DAYS_AGO] [--to_days_ago TO_DAYS_AGO] [--loglevel LOGLEVEL] [--timezone TIMEZONE]
                   [--base_url BASE_URL] [--octopus_api_key OCTOPUS_API_KEY] [--price_types PRICE_TYPES] [--usage USAGE] [--tariffs TARIFFS]
                   [--influx_database INFLUX_DATABASE] [--influx_tariff_measurement INFLUX_TARIFF_MEASUREMENT] [--influx_usage_measurement INFLUX_USAGE_MEASUREMENT]
                   [--influx_url INFLUX_URL] [--influx_api_token INFLUX_API_TOKEN]

Download usage and pricing data from the Octopus API
(https://developer.octopus.energy/docs/api/) and store into Influxdb.

options:
  -h, --help            show this help message and exit
  --from_max_days_ago FROM_MAX_DAYS_AGO
                        Get Octopus data from the last retrieved timestamp, but no more than this many days ago.
  --from_days_ago FROM_DAYS_AGO
                        Get Octopus data from that many days ago (0 means today). If set, this overrides from_max_days_ago.
  --to_days_ago TO_DAYS_AGO
                        Get Octopus data until that many days ago (0 means today).
  --loglevel LOGLEVEL   Level of logs (INFO, DEBUG, WARNING, ERROR).
  --timezone TIMEZONE   Timezone of the Octopus account (e.g. where you live). Most likely always "Europe/London".
  --base_url BASE_URL   Base URL of the Octopus API (e.g. "https://api.octopus.energy/v1").
  --octopus_api_key OCTOPUS_API_KEY
                        (**Config file or environment only**) The API Token to connect to the Octopus API. Can be generated on
                        https://octopus.energy/dashboard/developer/.
  --price_types PRICE_TYPES
                        (**Config only**) List of price types to retrieve using the Octopus API, and their units.
  --usage USAGE         (**Config only**) List of Octopus usage (electricity/gas import consumption, or export) to retrieve using the Octopus API.
  --tariffs TARIFFS     (**Config only**) List of Octopus tariffs to retrieve using the Octopus API.
  --influx_database INFLUX_DATABASE
                        InfluxDB 3 database name to store the data into (e.g. "octo2influx").
  --influx_tariff_measurement INFLUX_TARIFF_MEASUREMENT
                        InfluxDB 3 table (measurement) name to store tariff data into.
  --influx_usage_measurement INFLUX_USAGE_MEASUREMENT
                        InfluxDB 3 table (measurement) name to store consumption data into.
  --influx_url INFLUX_URL
                        URL of the InfluxDB 3 instance to store the data into (e.g. "http://localhost:8181")
  --influx_api_token INFLUX_API_TOKEN
                        (**Config file or environment only**) The API Token to connect to the InfluxDB 3 instance.

IMPORTANT NOTE: you should *not* define secrets and API tokens on the command
line, as it is unsecure (e.g. it may stay in your shell history, appear in
system audit logs, etc): you can define in an access-restricted configuration
file instead.

The settings can also be set in a config file (./config.yaml,
/etc/octo2influx/config.yaml, ~/.config/octo2influx/config.yaml,
or $OCTO2INFLUXDIR/config.yaml in a directory of your choice by defining
the env var OCTO2INFLUXDIR).
Or via environment variable of the form OCTO2INFLUX_COMMAND_LINE_ARG.
The priority from highest to lowest is: environment, command line, config file.
```

## Grafana dashboard

The bundled dashboard ([grafana/dashboard.json](grafana/dashboard.json)) was originally written for InfluxDB 2 and the **Flux** query language. InfluxDB 3 does *not* support Flux — it uses **SQL** (and InfluxQL). Its panels and template-variable queries have now been **converted to SQL**, so the only remaining step is to point Grafana at InfluxDB 3:

- Configure the Grafana InfluxDB data source to query InfluxDB 3 using **SQL** (FlightSQL), pointing at your `octo2influx` database and admin token, then import [grafana/dashboard.json](grafana/dashboard.json).

The queries use the `$__timeFilter(time)` macro and `date_bin()` for time bucketing. For reference, the *total electricity imported* over the dashboard time range is:

```sql
SELECT SUM("kWh") AS "Electricity imported"
FROM "octopus-usage"
WHERE "energy_type" = 'electricity'
  AND "direction" = 'import'
  AND $__timeFilter(time)
```

A tariff/usage time series (e.g. the import unit rate in £/kWh) becomes:

```sql
SELECT date_bin(INTERVAL '30 minutes', time) AS time,
       "p/kWh_inc_vat" / 100.0 AS "import-unit-rates_£/kWh"
FROM "octopus-tariffs"
WHERE "energy_type" = 'electricity' AND "direction" = 'import'
  AND "price_type" = 'standard-unit-rates'
  AND "tariff_code" = '${electricity_import_tariff}'
  AND $__timeFilter(time)
ORDER BY time
```

The per-interval **cost** panels combine usage with the tariff rate valid at each 30-minute interval. In InfluxDB 3 SQL this is done with `date_bin_gapfill()` and `locf()` (last-observation-carried-forward) to fill the sparse tariff series, for example:

```sql
WITH usage AS (
  SELECT date_bin_gapfill(INTERVAL '30 minutes', time) AS t,
         avg("kWh") AS kwh
  FROM "octopus-usage"
  WHERE "energy_type" = 'electricity' AND "direction" = 'import' AND $__timeFilter(time)
  GROUP BY t
),
rates AS (
  SELECT date_bin_gapfill(INTERVAL '30 minutes', time) AS t,
         locf(last_value("p/kWh_inc_vat")) AS unit_rate,
         locf(last_value("p/day_inc_vat")) AS standing
  FROM "octopus-tariffs"
  WHERE "energy_type" = 'electricity' AND "direction" = 'import'
    AND "tariff_code" = '${electricity_import_tariff}' AND $__timeFilter(time)
  GROUP BY t
)
SELECT SUM(coalesce(u.kwh, 0) * r.unit_rate) / 100.0 AS "Usage cost",
       SUM(r.standing) / 48.0 / 100.0            AS "Standing charge cost"
FROM usage u JOIN rates r ON u.t = r.t
```

> [!NOTE]
> The cost/revenue panels use InfluxDB 3's `date_bin_gapfill()` and `locf()` (last-observation-carried-forward) to spread the sparse tariff rates across each half-hour, and should be validated against your own data — cost figures depend on how your tariff rates line up with your half-hourly usage.

## Acknowledgement

This project was originally based on https://github.com/stevenewey/octograph/ which I used and found very useful: thanks @stevenewey. 

When I got Solar Panels I ended up largely rewritting it to be based on InfluxDB and the Influx query language, cover the electricty export too with a more advanced Grafana dashboard. It has since been migrated to InfluxDB 3 and SQL.
