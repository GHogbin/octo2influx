#!/usr/bin/python3

from influxdb_client_3 import InfluxDBClient3, InfluxDBError, Point
import dateutil.parser
from datetime import datetime, time, timedelta, timezone
import pytz
from pyarrow.flight import FlightError
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import argparse
import confuse
from dataclasses import dataclass, replace
from os import path
import logging
from time import perf_counter
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from octo2influx_core.costs import (
    build_cost_plan,
    compatible_tariffs,
    standing_charge_points,
    usage_cost_point,
)
from octo2influx_core.influx import (
    list_measurements,
    make_stream_id,
    query_watermark,
    sync_status_point,
    watermark_point,
    write_records,
)
from octo2influx_core.models import (
    RATE_TYPE_UNITS,
    RateBook,
    RatePeriod,
    TariffConfig,
    TariffSchedule,
    UsageConfig,
    infer_rate_types,
    parse_optional_datetime,
)
from octo2influx_core.octopus import (
    OctopusClient,
    discover_account_configuration,
)

PROGNAME = 'octo2influx'

logging.basicConfig(level=logging.INFO, datefmt='%Y-%m-%d %H:%M:%S', style='{',
                    format='{asctime} {levelname:>7} {filename}:{lineno:3}: {message}')


@dataclass(frozen=True)
class Parameter:
    arg_type: Callable[[str], Any] | type
    cfg_type: Any
    help: str
    default: Any = None
    validator: Callable[[Any], bool] | None = None


confuse_usage_template = {
    'energy_type': confuse.Choice(["electricity", "gas"]),
    'direction': confuse.Choice(["import", "export"]),
    'meter_point': str,  # MPAN for electricity, MPRN for gas
    'meter_serial': str,
    'unit': confuse.Choice(["kWh", "m3"]),
}


confuse_tariff_template = {
    'energy_type': confuse.Choice(["electricity", "gas"]),
    'direction': confuse.Choice(["import", "export"]),
    'product_code': str,
    'tariff_code': str,
    'full_name': str,
    'display_name': str,
    'description': str,
    'rate_types': confuse.Optional(confuse.Sequence(str), default=[]),
    'payment_method': confuse.Optional(str, default=None),
    'agreement_from': confuse.Optional(str, default=None),
    'agreement_to': confuse.Optional(str, default=None),
}


confuse_rate_period_template = {
    'price_type': str,
    'start': str,
    'end': str,
}


confuse_tariff_schedule_template = {
    'timezone': confuse.Optional(str, default='Europe/London'),
    'default_price_type': str,
    'periods': confuse.Sequence(confuse_rate_period_template),
}


def _secret_unsafe_on_cmdline(val: str):
    raise argparse.ArgumentTypeError(
        'Do not set secrets on the command line as it is not safe: they may be recorded in your shell history, system audit, etc. Use an access-restricted configuration file, or environment variables (e.g. when using Docker Compose).')


def _config_only(val: str):
    raise argparse.ArgumentTypeError(
        'this config key is only supported in a configuration file.')


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {'1', 'true', 'yes', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'off'}:
        return False
    raise argparse.ArgumentTypeError(
        f"expected a boolean value, got '{value}'")


params = {
    # Runtime parameters:
    'from_max_days_ago': Parameter(int, int, 'Get Octopus data from the last retrieved timestamp, but no more than this many days ago.', default=60, validator=lambda x: x >= 0),
    'from_days_ago': Parameter(int, int, 'Get Octopus data from that many days ago (0 means today). If set, this overrides from_max_days_ago.', validator=lambda x: x >= 0),
    'to_days_ago': Parameter(int, int, 'Get Octopus data until that many days ago (0 means today).', default=0, validator=lambda x: x >= 0),
    'loglevel': Parameter(str, confuse.Choice(['INFO', 'DEBUG', 'WARNING', 'ERROR']), 'Level of logs (INFO, DEBUG, WARNING, ERROR).', default='INFO'),
    'request_timeout_seconds': Parameter(int, int, 'Timeout in seconds for each Octopus API request.', default=30, validator=lambda x: x > 0),
    'request_max_retries': Parameter(int, int, 'Number of retries for transient Octopus API failures.', default=4, validator=lambda x: x >= 0),
    'request_max_pages': Parameter(int, int, 'Maximum pages followed for one Octopus API request.', default=1000, validator=lambda x: x > 0),

    # Octopus settings:
    'timezone': Parameter(str, str, 'Timezone of the Octopus account (e.g. where you live). Most likely always "Europe/London".', default="Europe/London"),
    'base_url': Parameter(str, str, 'Base URL of the Octopus API (e.g. "https://api.octopus.energy/v1").'),
    'octopus_api_key': Parameter(_secret_unsafe_on_cmdline, str, '(**Config file or environment only**) The API Token to connect to the Octopus API. Can be generated on https://octopus.energy/dashboard/developer/.'),
    'account_number': Parameter(_secret_unsafe_on_cmdline, str, '(**Config file or environment only**) Optional Octopus account number used to discover meters and tariffs.'),
    'discover_historical_tariffs': Parameter(_parse_bool, bool, 'Discover historical tariff agreements as well as current agreements.', default=False),
    'discovered_gas_unit': Parameter(str, confuse.Choice(['kWh', 'm3']), 'Unit used for gas meters discovered from an account.', default='m3'),
    'gas_m3_to_kwh_factor': Parameter(float, float, 'Optional conversion factor used to estimate gas costs when consumption is reported in m3.', validator=lambda x: x > 0),
    'price_types': Parameter(_config_only, confuse.MappingValues(str), '(**Config only**) Optional rate endpoint to unit overrides.', default=RATE_TYPE_UNITS),
    'usage': Parameter(_config_only, confuse.Sequence(confuse_usage_template), '(**Config only**) Explicit Octopus usage streams.', default=[]),
    'tariffs': Parameter(_config_only, confuse.Sequence(confuse_tariff_template), '(**Config only**) Explicit Octopus tariffs.', default=[]),
    'tariff_schedules': Parameter(_config_only, confuse.MappingValues(confuse_tariff_schedule_template), '(**Config only**) Local-time schedules for multi-rate tariffs.', default={}),

    # Influx settings:
    'influx_database': Parameter(str, str, 'InfluxDB 3 database name to store the data into (e.g. "octo2influx").'),
    'influx_tariff_measurement': Parameter(str, str, 'InfluxDB 3 table (measurement) name to store tariff data into.'),
    'influx_usage_measurement': Parameter(str, str, 'InfluxDB 3 table (measurement) name to store consumption data into.'),
    'influx_url': Parameter(str, str, 'URL of the InfluxDB 3 instance to store the data into (e.g. "http://localhost:8181")'),
    'influx_api_token': Parameter(_secret_unsafe_on_cmdline, str, '(**Config file or environment only**) The API Token to connect to the InfluxDB 3 instance.'),
    'influx_write_batch_size': Parameter(int, int, 'Maximum points written in each InfluxDB request.', default=5000, validator=lambda x: x > 0),
    'influx_cost_measurement': Parameter(str, str, 'InfluxDB 3 measurement containing materialized tariff costs.', default='octopus-costs'),
    'influx_watermark_measurement': Parameter(str, str, 'InfluxDB 3 measurement containing per-stream checkpoints.', default='octopus-watermarks'),
    'influx_status_measurement': Parameter(str, str, 'InfluxDB 3 measurement containing synchronization status.', default='octopus-sync-status'),
}

argparse_description = '''
Download usage and pricing data from the Octopus API
(https://developer.octopus.energy/docs/api/) and store into Influxdb.
'''

argparse_epilog = f'''
IMPORTANT NOTE: you should *not* define secrets and API tokens on the command
line, as it is unsecure (e.g. it may stay in your shell history, appear in
system audit logs, etc): you can define in an access-restricted configuration
file instead.

The settings can also be set in a config file (./{confuse.CONFIG_FILENAME},
/etc/{PROGNAME}/{confuse.CONFIG_FILENAME}, ~/.config/{PROGNAME}/{confuse.CONFIG_FILENAME},
or ${PROGNAME.upper()}DIR/{confuse.CONFIG_FILENAME} in a directory of your choice by defining
the env var {PROGNAME.upper()}DIR).
Or via environment variable of the form {PROGNAME.upper()}_COMMAND_LINE_ARG.
The priority from highest to lowest is: environment, command line, config file.
'''


class ValidatedConfiguration(confuse.Configuration):
    """A confuse.Configuration which transparently validates all items.

    Each item with a validator will be transparently validated when
    accessed, with a TypeError exception raised if invalid.
    """

    def __init__(self, params, *args, **kwargs):
        self.params = params
        super().__init__(*args, **kwargs)

    def get_validated(self, key: str):
        assert key in self.params, f"configuration '{key}' not found in params."
        value = super().__getitem__(key).get(self.params[key].cfg_type)
        if self.params[key].validator:
            try:
                is_valid = self.params[key].validator(value)
            except Exception as e:
                raise TypeError(
                    f"Configuration key '{key}' has an invalid value: {value}") from e
            if not is_valid:
                raise TypeError(
                    f"Configuration key '{key}' has an invalid value: {value}")

        return value

    def __getitem__(self, key: str):
        return self.get_validated(key)


def get_url_of_tariff(base_url: str, tariff: confuse.templates.AttrDict, price_type: str) -> str:
    return f"{base_url}/products/{tariff.product_code}/{tariff.energy_type}-tariffs/{tariff.tariff_code}/{price_type}/"


def get_url_of_consumption(base_url: str, usage: confuse.templates.AttrDict) -> str:
    """Get the URL to retrieve the consumption.

    Args:
      energy: electricty | gas
      admin_number: MPAN for electricity, MPRN for gas
    """
    return f"{base_url}/{usage.energy_type}-meter-points/{usage.meter_point}/meters/{usage.meter_serial}/consumption/"


def create_http_session(max_retries: int) -> requests.Session:
    """Create an HTTP session that retries transient GET failures."""
    retries = Retry(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        status=max_retries,
        allowed_methods=frozenset({'GET'}),
        status_forcelist=(429, 500, 502, 503, 504),
        backoff_factor=1,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session = requests.Session()
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def retrieve_paginated_data(
        api_key: str,
        url: str,
        from_iso8601: str,
        to_iso8601: str,
        session: requests.Session | None = None,
        timeout_seconds: int = 30,
        authenticated: bool = True,
        max_pages: int = 1000,
) -> list[dict]:
    """Compatibility wrapper that collects validated Octopus API pages."""
    if (dateutil.parser.isoparse(from_iso8601) >=
            dateutil.parser.isoparse(to_iso8601)):
        # Nothing new to fetch: the last stored point is already at (or after)
        # the requested `to` time. Octopus returns 400 for an empty/inverted
        # range, so we skip the request and return no results.
        logging.info(
            f'       ... nothing new to retrieve '
            f'(from {from_iso8601} >= to {to_iso8601}).')
        return []

    request_session = session or create_http_session(max_retries=0)
    close_session = session is None
    results = []
    try:
        client = OctopusClient(
            request_session,
            url,
            api_key,
            timeout_seconds,
            max_pages,
        )
        for page in client.iter_pages(
                url,
                {
                    'period_from': from_iso8601,
                    'period_to': to_iso8601,
                },
                authenticated=authenticated):
            results.extend(page.items)
    finally:
        if close_session:
            request_session.close()

    return results


def std_unit_rate_to_points(measurement: str, row: dict, price_type: str, unit: str, tariff: confuse.templates.AttrDict, from_dt: datetime, to_dt: datetime) -> list[Point]:
    """Convert a single Octopus API rate datapoint into multiple InfluxDB points for easier querying and charting.

    Points are emitted at the validity boundaries and once per local calendar
    day so dashboard queries can carry a long-lived fixed rate forward.
    """
    source_valid_from = dateutil.parser.isoparse(row['valid_from'])
    source_valid_to = (
        dateutil.parser.isoparse(row['valid_to'])
        if row.get('valid_to') else None
    )
    valid_from = max(from_dt, source_valid_from)
    valid_to = min(
        to_dt,
        (
            source_valid_to - timedelta(seconds=1)
            if source_valid_to else to_dt
        ),
    )
    if valid_from > valid_to:
        return []

    def rate2point(tstamp: datetime) -> Point:
        point = Point(measurement)\
            .tag("energy_type", tariff.energy_type)\
            .tag("direction", tariff.direction)\
            .tag("tariff_code", tariff.tariff_code)\
            .tag("price_type", price_type)\
            .tag("product_code", tariff.product_code)\
            .tag("display_name", tariff.display_name)\
            .field(f"{unit}_inc_vat", float(row["value_inc_vat"]))\
            .field(f"{unit}_exc_vat", float(row["value_exc_vat"]))\
            .field("value_inc_vat", float(row["value_inc_vat"]))\
            .field("value_exc_vat", float(row["value_exc_vat"]))\
            .field("unit", unit)\
            .field("source_valid_from", source_valid_from.isoformat())\
            .time(tstamp)
        if source_valid_to is not None:
            point.field("source_valid_to", source_valid_to.isoformat())
        if row.get('payment_method'):
            point.field("payment_method", str(row['payment_method']))
        return point

    cfg_timezone = pytz.timezone(cfg['timezone'])
    timestamps = [valid_from]
    cur_dt = valid_from
    while cur_dt < valid_to:
        next_local_date = (
            cur_dt.astimezone(cfg_timezone).date() + timedelta(days=1)
        )
        next_midnight = cfg_timezone.localize(
            datetime.combine(next_local_date, datetime.min.time())
        )
        if next_midnight >= valid_to:
            break
        timestamps.append(next_midnight)
        cur_dt = next_midnight

    if timestamps[-1] != valid_to:
        timestamps.append(valid_to)
    return [rate2point(timestamp) for timestamp in timestamps]


def consumption_to_point(measurement: str, row: dict, usage: confuse.templates.AttrDict) -> Point:
    """Convert a single Octopus API usage datapoint into an InfluxDB point."""
    # Example data from the Octopus API:
    # data=[
    # {'consumption': 0.001, 'interval_start': '2023-07-31T00:30:00+01:00', 'interval_end': '2023-07-31T01:00:00+01:00'},
    # {'consumption': 0.0, 'interval_start': '2023-07-31T00:00:00+01:00', 'interval_end': '2023-07-31T00:30:00+01:00'},
    # {'consumption': 0.0, 'interval_start': '2023-07-30T23:30:00+01:00', 'interval_end': '2023-07-31T00:00:00+01:00'},
    # ...
    # ]
    interval_start = dateutil.parser.isoparse(row["interval_start"])
    interval_end = dateutil.parser.isoparse(row["interval_end"])
    mid_dt = interval_start + (interval_end - interval_start) / 2
    consumption = float(row["consumption"])
    return Point(measurement) \
        .tag("energy_type", usage.energy_type)\
        .tag("direction", usage.direction)\
        .tag("meter_point", usage.meter_point)\
        .tag("meter_serial", usage.meter_serial)\
        .field("interval_start", interval_start.timestamp())\
        .field("interval_end", interval_end.timestamp())\
        .field(usage.unit, consumption)\
        .field("value", consumption)\
        .field("unit", usage.unit)\
        .time(mid_dt)


def iso8601_from_datetime(dt: datetime) -> str:
    """Convert a datetime into its iso8601 string representation."""
    dt_utc = dt.astimezone(pytz.utc)
    # We drop the timezone so there is no time offset +HH:MM suffix:
    return f"{dt_utc.replace(tzinfo=None).isoformat(timespec='seconds')}Z"


def datetime_days_ago(days_ago: int, time_of_day: datetime.time) -> datetime:
    """Return the timestamp of days_ago days ago from today at time_of_day."""
    cfg_timezone = pytz.timezone(cfg['timezone'])
    d = datetime.now(cfg_timezone).date() - timedelta(days=days_ago)
    return cfg_timezone.localize(datetime.combine(d, time_of_day))


def datetime_from_days_ago(days_ago: int) -> datetime:
    """Return the timestamp at 00:00 days_ago days ago."""
    return datetime_days_ago(days_ago, datetime.min.time())


def datetime_to_days_ago(days_ago: int) -> datetime:
    """Return the timestamp at 23:59 days_ago days ago."""
    return datetime_days_ago(days_ago, datetime.max.time())


def quote_sql_identifier(identifier: str) -> str:
    """Quote an InfluxDB SQL identifier."""
    if not identifier:
        raise ValueError('SQL identifiers cannot be empty.')
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def quote_sql_string(value: str) -> str:
    """Quote an InfluxDB SQL string literal."""
    return f"'{value.replace(chr(39), chr(39) * 2)}'"


def _read_query_result(result):
    """Return a pyarrow table from either supported query result shape."""
    return result.read_all() if hasattr(result, "read_all") else result


def list_influx_measurements(client: InfluxDBClient3) -> set[str]:
    """Return the measurements currently present in the configured database."""
    result = client.query(
        query='''
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'iox'
        ''',
        language='sql',
    )
    table = _read_query_result(result)
    if not table.num_rows:
        return set()
    return set(table.column('table_name').to_pylist())


def _query_last_datetime(
        client: InfluxDBClient3,
        sql: str,
) -> datetime | None:
    result = client.query(query=sql, language="sql")
    table = _read_query_result(result)
    last_dt = table.column("last_time")[0].as_py() if table.num_rows else None
    if last_dt is not None and last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return last_dt


def query_last_datetime(client: InfluxDBClient3,
                        sql: str, from_max_days_ago: int) -> datetime:
    """Return the timestamp of the most recent point matching the SQL query.

    The SQL query must select a single column aliased `last_time` (e.g.
    MAX("time")). The function will look for data at most from_max_days_ago old.
    If no matching row is found, it returns the timestamp from
    from_max_days_ago. Query failures are surfaced to the caller.
    """
    last_dt = _query_last_datetime(client, sql)
    if last_dt is None:
        return datetime_from_days_ago(from_max_days_ago)
    return last_dt


def query_last_datetime_in_windows(
        client: InfluxDBClient3,
        sql: str,
        from_max_days_ago: int,
        window_hours: int = 6,
) -> datetime:
    """Find the latest legacy point without scanning every Parquet file."""
    total_hours = from_max_days_ago * 24
    for newer_hours_ago in range(0, total_hours, window_hours):
        older_hours_ago = min(
            newer_hours_ago + window_hours,
            total_hours,
        )
        window_sql = f'''
            {sql}
              AND "time" >= now() - INTERVAL '{older_hours_ago} hours'
              AND "time" < now() - INTERVAL '{newer_hours_ago} hours'
        '''
        last_dt = _query_last_datetime(client, window_sql)
        if last_dt is not None:
            return last_dt
    return datetime_from_days_ago(from_max_days_ago)


def tariff_last_datetime(client: InfluxDBClient3,
                         from_max_days_ago: int,
                         influx_measurement: str, energy_type: str,
                         price_type: str,
                         tariff_code: str) -> datetime:
    """Return the timestamp of the most recent tariff point from InfluxDB.

    The function will look for data at most from_max_days_ago old. If none is found,
    it will return the timestamp from from_max_days_ago.
    """
    measurement = quote_sql_identifier(influx_measurement)
    sql = f'''
        SELECT MAX("time") AS last_time
        FROM {measurement}
        WHERE "energy_type" = {quote_sql_string(energy_type)}
          AND "price_type" = {quote_sql_string(price_type)}
          AND "tariff_code" = {quote_sql_string(tariff_code)}
    '''
    return query_last_datetime_in_windows(
        client, sql, from_max_days_ago)


def consumption_last_iso8601(client: InfluxDBClient3,
                             from_max_days_ago: int,
                             influx_measurement: str,
                             direction: str,
                             meter_point: str, meter_serial: str) -> str:
    """Return the timestamp of the most recent consumption point, in ISO8601 format.

    The function will look for data at most from_max_days_ago old. If none is found,
    it will return the timestamp from from_max_days_ago.
    """
    measurement = quote_sql_identifier(influx_measurement)
    sql = f'''
        SELECT MAX("time") AS last_time
        FROM {measurement}
        WHERE "direction" = {quote_sql_string(direction)}
          AND "meter_point" = {quote_sql_string(meter_point)}
          AND "meter_serial" = {quote_sql_string(meter_serial)}
    '''
    last_dt = query_last_datetime_in_windows(
        client, sql, from_max_days_ago)
    return iso8601_from_datetime(last_dt)


def write_points(client: InfluxDBClient3, points: list[Point],
                 batch_size: int) -> None:
    """Write points in bounded batches."""
    for start in range(0, len(points), batch_size):
        client.write(record=points[start:start + batch_size])


def build_argparser(params: dict[str, Parameter]) -> argparse.ArgumentParser:
    """Build and return a command line argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROGNAME,
        description=argparse_description,
        epilog=argparse_epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    for name, parameter in params.items():
        default = argparse.SUPPRESS
        if parameter.default is not None:
            try:
                default = cfg[name]
            except confuse.exceptions.NotFoundError:
                default = parameter.default
        parser.add_argument(
            f'--{name}', type=parameter.arg_type, help=parameter.help, default=default)

    return parser


cfg = ValidatedConfiguration(params, PROGNAME, __name__)


def optional_config(key: str, default=None):
    try:
        return cfg[key]
    except confuse.exceptions.NotFoundError:
        return default


def explicit_usage_configs() -> list[UsageConfig]:
    return [
        UsageConfig(
            energy_type=item.energy_type,
            direction=item.direction,
            meter_point=item.meter_point,
            meter_serial=item.meter_serial,
            unit=item.unit,
        )
        for item in cfg['usage']
    ]


def explicit_tariff_configs() -> list[TariffConfig]:
    tariffs = []
    for item in cfg['tariffs']:
        tariffs.append(TariffConfig(
            energy_type=item.energy_type,
            direction=item.direction,
            product_code=item.product_code,
            tariff_code=item.tariff_code,
            full_name=item.full_name,
            display_name=item.display_name,
            description=item.description,
            rate_types=tuple(item.rate_types),
            payment_method=item.payment_method,
            agreement_from=parse_optional_datetime(item.agreement_from),
            agreement_to=parse_optional_datetime(item.agreement_to),
        ))
    return tariffs


def configured_tariff_schedules() -> dict[str, TariffSchedule]:
    schedules = {}
    for tariff_code, item in cfg['tariff_schedules'].items():
        try:
            timezone_name = item.timezone
            ZoneInfo(timezone_name)
            periods = tuple(
                RatePeriod(
                    price_type=period.price_type,
                    start=time.fromisoformat(period.start),
                    end=time.fromisoformat(period.end),
                )
                for period in item.periods
            )
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError(
                f'Invalid schedule for tariff {tariff_code}: {error}'
            ) from error
        schedules[tariff_code] = TariffSchedule(
            timezone_name=timezone_name,
            default_price_type=item.default_price_type,
            periods=periods,
        )
    return schedules


def merge_usage_configs(
        explicit: list[UsageConfig],
        discovered: list[UsageConfig],
) -> list[UsageConfig]:
    merged = []
    seen = set()
    for item in [*explicit, *discovered]:
        if item.key not in seen:
            merged.append(item)
            seen.add(item.key)
    return merged


def merge_tariff_configs(
        explicit: list[TariffConfig],
        discovered: list[TariffConfig],
) -> list[TariffConfig]:
    explicit_keys = {item.key for item in explicit}
    merged = list(explicit)
    discovered_indexes: dict[tuple[str, str, str], int] = {}
    for item in discovered:
        if item.key in explicit_keys:
            continue
        if item.key not in discovered_indexes:
            discovered_indexes[item.key] = len(merged)
            merged.append(item)
            continue

        index = discovered_indexes[item.key]
        existing = merged[index]
        windows = []
        for window in [
                *existing.validity_windows,
                *item.validity_windows]:
            if window not in windows:
                windows.append(window)
        rate_types = tuple(dict.fromkeys(
            (*existing.rate_types, *item.rate_types)
        ))
        merged[index] = replace(
            existing,
            agreement_from=None,
            agreement_to=None,
            agreement_windows=tuple(windows),
            rate_types=rate_types,
        )
    return merged


def resolve_sync_configuration(
        octopus_client: OctopusClient,
) -> tuple[list[UsageConfig], list[TariffConfig]]:
    explicit_usage = explicit_usage_configs()
    explicit_tariffs = explicit_tariff_configs()
    account_number = optional_config('account_number')
    if not account_number:
        return explicit_usage, explicit_tariffs

    discovered_usage, discovered_tariffs = discover_account_configuration(
        octopus_client,
        account_number,
        cfg['discovered_gas_unit'],
        cfg['discover_historical_tariffs'],
    )
    return (
        merge_usage_configs(explicit_usage, discovered_usage),
        merge_tariff_configs(explicit_tariffs, discovered_tariffs),
    )


def validate_stream_configuration(
        parser: argparse.ArgumentParser,
        usage_items: list[UsageConfig],
        tariff_items: list[TariffConfig],
) -> None:
    if not usage_items and not tariff_items:
        parser.error(
            'Configure usage/tariffs explicitly or provide account_number.')

    seen_usage = set()
    for usage in usage_items:
        if usage.key in seen_usage:
            parser.error(f'Duplicate usage stream: {usage.key}.')
        seen_usage.add(usage.key)
        if usage.energy_type == 'electricity' and usage.unit != 'kWh':
            parser.error('Electricity usage must use kWh.')
        if usage.energy_type == 'gas' and usage.direction != 'import':
            parser.error('Gas export streams are not supported.')
        if (
                usage.meter_point.startswith('YOUR_')
                or usage.meter_serial.startswith('YOUR_')):
            parser.error(
                'Replace all YOUR_* meter placeholders in config.yaml.')

    seen_tariffs = set()
    for tariff in tariff_items:
        if tariff.key in seen_tariffs:
            parser.error(f'Duplicate tariff: {tariff.key}.')
        seen_tariffs.add(tariff.key)
        expected_prefix = 'E-' if tariff.energy_type == 'electricity' else 'G-'
        if not tariff.tariff_code.startswith(expected_prefix):
            parser.error(
                f'Tariff {tariff.tariff_code} does not match '
                f'energy type {tariff.energy_type}.')
        for valid_from, valid_to in tariff.validity_windows:
            if (
                    valid_from is not None
                    and valid_to is not None
                    and valid_from >= valid_to):
                parser.error(
                    f'Tariff {tariff.tariff_code} has an inverted '
                    'agreement range.')


def validate_configuration(parser: argparse.ArgumentParser) -> None:
    """Validate required settings and relationships between them."""
    required_keys = (
        'base_url',
        'octopus_api_key',
        'price_types',
        'usage',
        'tariffs',
        'influx_database',
        'influx_tariff_measurement',
        'influx_usage_measurement',
        'influx_url',
        'influx_api_token',
        'timezone',
        'from_max_days_ago',
        'to_days_ago',
        'request_timeout_seconds',
        'request_max_retries',
        'request_max_pages',
        'influx_write_batch_size',
        'influx_cost_measurement',
        'influx_watermark_measurement',
        'influx_status_measurement',
        'discover_historical_tariffs',
        'discovered_gas_unit',
        'tariff_schedules',
    )
    try:
        for key in required_keys:
            cfg[key]
        pytz.timezone(cfg['timezone'])
    except (confuse.exceptions.ConfigError, TypeError,
            pytz.UnknownTimeZoneError) as error:
        parser.error(str(error))

    try:
        from_days_ago = cfg['from_days_ago']
    except confuse.exceptions.NotFoundError:
        from_days_ago = None

    if (from_days_ago is not None and
            from_days_ago < cfg['to_days_ago']):
        parser.error(
            'from_days_ago must be greater than or equal to to_days_ago.')

    if cfg['octopus_api_key'].startswith('MY_'):
        parser.error('Replace MY_OCTOPUS_API_KEY in config.yaml.')
    if cfg['influx_api_token'].startswith('MY_'):
        parser.error('Replace MY_INFLUX_API_TOKEN in config.yaml.')

    try:
        configured_tariff_schedules()
    except ValueError as error:
        parser.error(str(error))


@dataclass(frozen=True)
class StreamFailure:
    stream: str
    error: Exception


class SynchronizationError(RuntimeError):
    def __init__(self, failures: list[StreamFailure]):
        self.failures = failures
        details = '; '.join(
            f'{failure.stream}: {failure.error}' for failure in failures
        )
        super().__init__(
            f'{len(failures)} synchronization stream(s) failed: {details}')


STREAM_ERRORS = (
    requests.RequestException,
    InfluxDBError,
    FlightError,
    KeyError,
    TypeError,
    ValueError,
)


def usage_stream_id(usage: UsageConfig) -> str:
    return make_stream_id('usage', *usage.key)


def tariff_stream_id(tariff: TariffConfig, price_type: str) -> str:
    return make_stream_id('tariff', *tariff.key, price_type)


def usage_cost_stream_id(
        usage: UsageConfig,
        tariff: TariffConfig,
) -> str:
    return make_stream_id('cost-usage', *usage.key, *tariff.key)


def standing_cost_stream_id(
        usage: UsageConfig,
        tariff: TariffConfig,
) -> str:
    return make_stream_id(
        'cost-standing',
        usage.energy_type,
        usage.direction,
        usage.meter_point,
        *tariff.key,
    )


def configured_from_datetime() -> datetime | None:
    from_days_ago = optional_config('from_days_ago')
    if from_days_ago is None:
        return None
    return datetime_from_days_ago(from_days_ago)


def stored_watermark(
        client: InfluxDBClient3,
        existing_measurements: set[str],
        stream_id: str,
) -> datetime | None:
    measurement = cfg['influx_watermark_measurement']
    if measurement not in existing_measurements:
        return None
    return query_watermark(client, measurement, stream_id)


def default_from_datetime() -> datetime:
    return datetime_from_days_ago(cfg['from_max_days_ago'])


def usage_raw_start(
        client: InfluxDBClient3,
        existing_measurements: set[str],
        usage: UsageConfig,
        configured_from: datetime | None,
) -> datetime:
    if configured_from is not None:
        return configured_from
    watermark = stored_watermark(
        client, existing_measurements, usage_stream_id(usage))
    if watermark is not None:
        return watermark
    if cfg['influx_usage_measurement'] in existing_measurements:
        return dateutil.parser.isoparse(consumption_last_iso8601(
            client,
            cfg['from_max_days_ago'],
            cfg['influx_usage_measurement'],
            usage.direction,
            usage.meter_point,
            usage.meter_serial,
        ))
    return default_from_datetime()


def tariff_raw_start(
        client: InfluxDBClient3,
        existing_measurements: set[str],
        tariff: TariffConfig,
        price_type: str,
        configured_from: datetime | None,
) -> datetime:
    if configured_from is not None:
        return configured_from
    watermark = stored_watermark(
        client,
        existing_measurements,
        tariff_stream_id(tariff, price_type),
    )
    if watermark is not None:
        return watermark
    # Legacy tariff rows may contain generated future boundary points. They are
    # not reliable checkpoints, so the first watermark-enabled run replays the
    # bounded lookback and establishes an explicit source-coverage watermark.
    return default_from_datetime()


def cost_stream_start(
        client: InfluxDBClient3,
        existing_measurements: set[str],
        stream_id: str,
        configured_from: datetime | None,
) -> datetime:
    if configured_from is not None:
        return configured_from
    return (
        stored_watermark(client, existing_measurements, stream_id)
        or default_from_datetime()
    )


def rate_unit(price_type: str) -> str:
    configured_units = dict(cfg['price_types'])
    if price_type in configured_units:
        return configured_units[price_type]
    if price_type in RATE_TYPE_UNITS:
        return RATE_TYPE_UNITS[price_type]
    raise ValueError(f'No unit configured for rate type {price_type}.')


def effective_tariff_range(
        tariff: TariffConfig,
        from_dt: datetime,
        to_dt: datetime,
) -> tuple[datetime, datetime]:
    return tariff.coverage_bounds(from_dt, to_dt)


def rate_coverage_end(rows: list[dict], to_dt: datetime) -> datetime | None:
    coverage = []
    for row in rows:
        valid_from = parse_optional_datetime(row.get('valid_from'))
        valid_to = parse_optional_datetime(row.get('valid_to'))
        if valid_to is None and valid_from is not None:
            coverage.append(to_dt)
        elif valid_to is not None:
            coverage.append(min(valid_to, to_dt))
    return max(coverage) if coverage else None


def latest_consumption_end(rows: list[dict]) -> datetime | None:
    timestamps = [
        dateutil.parser.isoparse(row['interval_end']) for row in rows
    ]
    return max(timestamps) if timestamps else None


def sync_data(
        client: InfluxDBClient3,
        octopus_client: OctopusClient,
        usage_items: list[UsageConfig],
        tariff_items: list[TariffConfig],
) -> None:
    """Synchronize independent streams and report all failures together."""
    started_at = perf_counter()
    failures: dict[str, StreamFailure] = {}
    successful_streams: set[str] = set()
    to_dt = datetime_to_days_ago(cfg['to_days_ago'])
    to_iso8601 = iso8601_from_datetime(to_dt)
    configured_from = configured_from_datetime()
    if configured_from is not None:
        logging.info(
            f'`from_days_ago` is defined: retrieving from '
            f'{cfg["from_days_ago"]} days ago.')

    existing_measurements = list_measurements(client)
    watermark_measurement = cfg['influx_watermark_measurement']
    usage_measurement = cfg['influx_usage_measurement']
    tariff_measurement = cfg['influx_tariff_measurement']
    cost_measurement = cfg['influx_cost_measurement']
    batch_size = cfg['influx_write_batch_size']
    schedules = configured_tariff_schedules()
    gas_conversion_factor = optional_config('gas_m3_to_kwh_factor')

    def remember_failure(
            stream: str,
            error: Exception,
            stream_id: str | None = None,
    ) -> None:
        if stream not in failures:
            failures[stream] = StreamFailure(stream, error)
        if stream_id is not None:
            successful_streams.discard(stream_id)
        logging.error(f'{stream} failed: {error}', exc_info=True)

    usage_fetch_starts: dict[UsageConfig, datetime] = {}
    standing_starts: dict[
        str, tuple[UsageConfig, TariffConfig, datetime]
    ] = {}

    for usage in usage_items:
        raw_start = usage_raw_start(
            client, existing_measurements, usage, configured_from)
        required_starts = [raw_start]
        for tariff in compatible_tariffs(usage, tariff_items):
            usage_cost_start = cost_stream_start(
                client,
                existing_measurements,
                usage_cost_stream_id(usage, tariff),
                configured_from,
            )
            standing_start = cost_stream_start(
                client,
                existing_measurements,
                standing_cost_stream_id(usage, tariff),
                configured_from,
            )
            required_starts.extend((usage_cost_start, standing_start))
            standing_stream = standing_cost_stream_id(usage, tariff)
            existing = standing_starts.get(standing_stream)
            if existing is None or standing_start < existing[2]:
                standing_starts[standing_stream] = (
                    usage,
                    tariff,
                    standing_start,
                )
        usage_fetch_starts[usage] = min(required_starts)

    rate_books = {
        tariff.key: RateBook() for tariff in tariff_items
    }

    logging.info('=== Retrieving tariffs...')
    for tariff in tariff_items:
        related_usage = [
            usage for usage in usage_items
            if usage.energy_type == tariff.energy_type
            and usage.direction == tariff.direction
        ]
        for price_type in infer_rate_types(tariff):
            stream_id = tariff_stream_id(tariff, price_type)
            stream_name = (
                f'tariff {tariff.tariff_code} {price_type}'
            )
            try:
                raw_start = tariff_raw_start(
                    client,
                    existing_measurements,
                    tariff,
                    price_type,
                    configured_from,
                )
                required_starts = [
                    usage_fetch_starts[usage] for usage in related_usage
                ]
                from_dt = min([raw_start, *required_starts])
                from_dt, effective_to = effective_tariff_range(
                    tariff, from_dt, to_dt)
                if from_dt >= effective_to:
                    successful_streams.add(stream_id)
                    continue

                logging.info(
                    f'====== Retrieving {tariff.energy_type} {price_type} '
                    f'price of tariff {tariff.full_name} from Octopus...')
                logging.debug(
                    f'from {from_dt} to {effective_to}')
                rows = []
                for page in octopus_client.rate_pages(
                        tariff,
                        price_type,
                        iso8601_from_datetime(from_dt),
                        iso8601_from_datetime(effective_to)):
                    rows.extend(page.items)

                if not rows:
                    raise ValueError(
                        f'Octopus returned no rates for {stream_name}.')
                rate_books[tariff.key].add_rows(price_type, rows)
                coverage_end = rate_coverage_end(rows, effective_to)
                if coverage_end is None:
                    raise ValueError(
                        f'Could not determine coverage for {stream_name}.')

                points = []
                for row in sorted(
                        rows,
                        key=lambda item: item.get('valid_from') or ''):
                    points.extend(std_unit_rate_to_points(
                        tariff_measurement,
                        row,
                        price_type,
                        rate_unit(price_type),
                        tariff,
                        from_dt,
                        coverage_end,
                    ))
                checkpoint = watermark_point(
                    watermark_measurement,
                    stream_id,
                    'tariff',
                    coverage_end,
                    len(rows),
                )
                write_records(
                    client, points, batch_size, checkpoint)
                existing_measurements.update(
                    (tariff_measurement, watermark_measurement))
                successful_streams.add(stream_id)
                logging.info(
                    f'       ... {len(points)} tariff points written.')
            except STREAM_ERRORS as error:
                remember_failure(stream_name, error, stream_id)

    logging.info('=== Retrieving consumption...')
    disabled_cost_streams: set[str] = set()
    for usage in usage_items:
        stream_id = usage_stream_id(usage)
        stream_name = (
            f'usage {usage.energy_type} {usage.direction} '
            f'{usage.meter_point}/{usage.meter_serial}'
        )
        from_dt = usage_fetch_starts[usage]
        if from_dt >= to_dt:
            successful_streams.add(stream_id)
            continue

        plans = []
        for tariff in compatible_tariffs(usage, tariff_items):
            plan, reason = build_cost_plan(
                usage,
                tariff,
                rate_books[tariff.key],
                schedules,
                gas_conversion_factor,
            )
            if plan is not None:
                plans.append(plan)
            elif reason:
                logging.warning(
                    f'Cost materialization skipped for '
                    f'{usage.meter_point} and {tariff.tariff_code}: '
                    f'{reason}')

        try:
            logging.info(
                f'====== Retrieving {usage.energy_type} {usage.direction} '
                f'({usage.meter_point}) from Octopus...')
            logging.debug(f'from {from_dt} to {to_dt}')
            received_rows = 0
            for page in octopus_client.consumption_pages(
                    usage,
                    iso8601_from_datetime(from_dt),
                    to_iso8601):
                rows = page.items
                if not rows:
                    continue
                received_rows += len(rows)
                latest_end = latest_consumption_end(rows)
                if latest_end is None:
                    raise ValueError(
                        f'No interval_end found for {stream_name}.')

                raw_points = [
                    consumption_to_point(usage_measurement, row, usage)
                    for row in rows
                ]
                raw_checkpoint = watermark_point(
                    watermark_measurement,
                    stream_id,
                    'usage',
                    latest_end,
                    len(rows),
                )
                write_records(
                    client,
                    raw_points,
                    batch_size,
                    raw_checkpoint,
                )
                existing_measurements.update(
                    (usage_measurement, watermark_measurement))

                for plan in plans:
                    cost_stream = usage_cost_stream_id(
                        usage, plan.tariff)
                    if cost_stream in disabled_cost_streams:
                        continue
                    cost_name = (
                        f'cost {usage.meter_point} '
                        f'{plan.tariff.tariff_code}'
                    )
                    try:
                        cost_points = []
                        missing_rates = 0
                        for row in rows:
                            interval_start = dateutil.parser.isoparse(
                                row['interval_start'])
                            point = usage_cost_point(
                                cost_measurement, row, plan)
                            if point is not None:
                                cost_points.append(point)
                            elif plan.tariff.applies_at(interval_start):
                                missing_rates += 1

                        if missing_rates:
                            write_records(
                                client, cost_points, batch_size)
                            disabled_cost_streams.add(cost_stream)
                            raise ValueError(
                                f'No applicable rate for {missing_rates} '
                                'consumption interval(s).')

                        cost_checkpoint = watermark_point(
                            watermark_measurement,
                            cost_stream,
                            'cost-usage',
                            latest_end,
                            len(cost_points),
                        )
                        write_records(
                            client,
                            cost_points,
                            batch_size,
                            cost_checkpoint,
                        )
                        existing_measurements.update(
                            (cost_measurement, watermark_measurement))
                        successful_streams.add(cost_stream)
                    except STREAM_ERRORS as error:
                        disabled_cost_streams.add(cost_stream)
                        remember_failure(
                            cost_name, error, cost_stream)

            successful_streams.add(stream_id)
            logging.info(
                f'       ... {received_rows} consumption rows processed.')
        except STREAM_ERRORS as error:
            remember_failure(stream_name, error, stream_id)

    logging.info('=== Materializing daily standing charges...')
    for stream_id, (usage, tariff, from_dt) in standing_starts.items():
        stream_name = (
            f'standing cost {usage.meter_point} {tariff.tariff_code}'
        )
        try:
            from_dt, effective_to = effective_tariff_range(
                tariff, from_dt, to_dt)
            if from_dt >= effective_to:
                successful_streams.add(stream_id)
                continue
            points = standing_charge_points(
                cost_measurement,
                usage,
                tariff,
                rate_books[tariff.key],
                from_dt,
                effective_to,
                cfg['timezone'],
            )
            if not points:
                raise ValueError(
                    f'No standing-charge rates found for {stream_name}.')
            checkpoint = watermark_point(
                watermark_measurement,
                stream_id,
                'cost-standing',
                effective_to,
                len(points),
            )
            write_records(client, points, batch_size, checkpoint)
            existing_measurements.update(
                (cost_measurement, watermark_measurement))
            successful_streams.add(stream_id)
        except STREAM_ERRORS as error:
            remember_failure(stream_name, error, stream_id)

    duration = perf_counter() - started_at
    status = sync_status_point(
        cfg['influx_status_measurement'],
        len(successful_streams),
        len(failures),
        duration,
        '; '.join(
            f'{failure.stream}: {failure.error}'
            for failure in failures.values()
        ),
    )
    write_records(client, [status], batch_size)

    if failures:
        raise SynchronizationError(list(failures.values()))


def main() -> None:
    """Load configuration and run one synchronization."""
    # Confuse automatically tries to load config.yaml from a number of
    # locations. Also try to load a config file in the same directory:
    local_config_path = path.join(path.realpath(
        path.dirname(__file__)), confuse.CONFIG_FILENAME)
    read_local_config = False
    if path.isfile(local_config_path):
        cfg.set_file(local_config_path)
        read_local_config = True

    parser = build_argparser(params)
    args = parser.parse_args()
    cfg.set_args(args)
    cfg.set_env()
    validate_configuration(parser)
    logging.root.setLevel(cfg['loglevel'])

    if read_local_config:
        logging.info(f'Read configuration from {local_config_path}.')

    with create_http_session(cfg['request_max_retries']) as http_session:
        octopus_client = OctopusClient(
            http_session,
            cfg['base_url'],
            cfg['octopus_api_key'],
            cfg['request_timeout_seconds'],
            cfg['request_max_pages'],
        )
        usage_items, tariff_items = resolve_sync_configuration(
            octopus_client)
        validate_stream_configuration(
            parser, usage_items, tariff_items)
        logging.info(
            f'Using {len(usage_items)} usage stream(s) and '
            f'{len(tariff_items)} tariff(s).')

        with InfluxDBClient3(
                host=cfg['influx_url'],
                token=cfg['influx_api_token'],
                database=cfg['influx_database']) as client:
            sync_data(
                client,
                octopus_client,
                usage_items,
                tariff_items,
            )


if __name__ == "__main__":
    main()
