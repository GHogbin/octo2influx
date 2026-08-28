#!/usr/bin/python3

from influxdb_client_3 import InfluxDBClient3, Point
import dateutil.parser
from datetime import datetime, timedelta, timezone
import pytz
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import argparse
import confuse
from dataclasses import dataclass
from os import path
import logging
from typing import Any, Callable

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
}


def _secret_unsafe_on_cmdline(val: str):
    raise argparse.ArgumentTypeError(
        'Do not set secrets on the command line as it is not safe: they may be recorded in your shell history, system audit, etc. Use an access-restricted configuration file, or environment variables (e.g. when using Docker Compose).')


def _config_only(val: str):
    raise argparse.ArgumentTypeError(
        'this config key is only supported in a configuration file.')


params = {
    # Runtime parameters:
    'from_max_days_ago': Parameter(int, int, 'Get Octopus data from the last retrieved timestamp, but no more than this many days ago.', default=60, validator=lambda x: x >= 0),
    'from_days_ago': Parameter(int, int, 'Get Octopus data from that many days ago (0 means today). If set, this overrides from_max_days_ago.', validator=lambda x: x >= 0),
    'to_days_ago': Parameter(int, int, 'Get Octopus data until that many days ago (0 means today).', default=0, validator=lambda x: x >= 0),
    'loglevel': Parameter(str, confuse.Choice(['INFO', 'DEBUG', 'WARNING', 'ERROR']), 'Level of logs (INFO, DEBUG, WARNING, ERROR).', default='INFO'),
    'request_timeout_seconds': Parameter(int, int, 'Timeout in seconds for each Octopus API request.', default=30, validator=lambda x: x > 0),
    'request_max_retries': Parameter(int, int, 'Number of retries for transient Octopus API failures.', default=4, validator=lambda x: x >= 0),

    # Octopus settings:
    'timezone': Parameter(str, str, 'Timezone of the Octopus account (e.g. where you live). Most likely always "Europe/London".', default="Europe/London"),
    'base_url': Parameter(str, str, 'Base URL of the Octopus API (e.g. "https://api.octopus.energy/v1").'),
    'octopus_api_key': Parameter(_secret_unsafe_on_cmdline, str, '(**Config file or environment only**) The API Token to connect to the Octopus API. Can be generated on https://octopus.energy/dashboard/developer/.'),
    'price_types': Parameter(_config_only, confuse.MappingValues(str), '(**Config only**) List of price types to retrieve using the Octopus API, and their units.'),
    'usage': Parameter(_config_only, confuse.Sequence(confuse_usage_template), '(**Config only**) List of Octopus usage (electricity/gas import consumption, or export) to retrieve using the Octopus API.'),
    'tariffs': Parameter(_config_only, confuse.Sequence(confuse_tariff_template), '(**Config only**) List of Octopus tariffs to retrieve using the Octopus API.'),

    # Influx settings:
    'influx_database': Parameter(str, str, 'InfluxDB 3 database name to store the data into (e.g. "octo2influx").'),
    'influx_tariff_measurement': Parameter(str, str, 'InfluxDB 3 table (measurement) name to store tariff data into.'),
    'influx_usage_measurement': Parameter(str, str, 'InfluxDB 3 table (measurement) name to store consumption data into.'),
    'influx_url': Parameter(str, str, 'URL of the InfluxDB 3 instance to store the data into (e.g. "http://localhost:8181")'),
    'influx_api_token': Parameter(_secret_unsafe_on_cmdline, str, '(**Config file or environment only**) The API Token to connect to the InfluxDB 3 instance.'),
    'influx_write_batch_size': Parameter(int, int, 'Maximum points written in each InfluxDB request.', default=5000, validator=lambda x: x > 0),
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
) -> list[dict]:
    """Retrieve every page in an Octopus API time range."""
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
    current_url = url
    current_params = {
        'period_from': from_iso8601,
        'period_to': to_iso8601,
    }
    results = []
    show_progress = logging.getLogger().isEnabledFor(logging.INFO)

    if show_progress:
        print('    progress (one dot per page) ', end='', flush=True)

    try:
        while current_url:
            response = request_session.get(
                current_url,
                params=current_params,
                auth=(api_key, ''),
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            page_results = data.get('results')
            if not isinstance(page_results, list):
                raise ValueError(
                    f'Octopus API response from {current_url} has no results list.')

            results.extend(page_results)
            current_url = data.get('next')
            if current_url is not None and not isinstance(current_url, str):
                raise ValueError(
                    f'Octopus API response from {url} has an invalid next link.')
            current_params = None

            if show_progress:
                print('.', end='', flush=True)
    finally:
        if show_progress:
            print()
        if close_session:
            request_session.close()

    return results


def std_unit_rate_to_points(measurement: str, row: dict, price_type: str, unit: str, tariff: confuse.templates.AttrDict, from_dt: datetime, to_dt: datetime) -> list[Point]:
    """Convert a single Octopus API rate datapoint into multiple InfluxDB points for easier querying and charting.

    Points are emitted at the validity boundaries and once per local calendar
    day so dashboard queries can carry a long-lived fixed rate forward.
    """

    # Example data from the Octopus API:
    # [
    #     {
    #       "value_exc_vat": 23.6849,
    #       "value_inc_vat": 23.6849,
    #       "valid_from": "2023-06-02T18:00:00Z",
    #       "valid_to": "2023-06-03T01:00:00Z",
    #       "payment_method": null
    #     },
    #     {
    #       "value_exc_vat": 37.5588,
    #       "value_inc_vat": 37.5588,
    #       "valid_from": "2023-06-02T15:00:00Z",
    #       "valid_to": "2023-06-02T18:00:00Z",
    #       "payment_method": null
    #     }
    # ]

    def rate2point(tstamp: datetime) -> Point:
        return Point(measurement)\
            .tag("energy_type", tariff.energy_type)\
            .tag("direction", tariff.direction)\
            .tag("tariff_code", tariff.tariff_code)\
            .tag("price_type", price_type)\
            .tag("product_code", tariff.product_code)\
            .tag("display_name", tariff.display_name)\
            .field(f"{unit}_inc_vat", row["value_inc_vat"])\
            .field(f"{unit}_exc_vat", row["value_exc_vat"])\
            .time(tstamp)

    valid_from = from_dt
    if "valid_from" in row and row["valid_from"]:
        point_valid_from = dateutil.parser.isoparse(row["valid_from"])
        # Don't allow points older than from_dt or it might go beyond the Influxdb retention and error:
        if point_valid_from > from_dt:
            valid_from = point_valid_from

    valid_to = to_dt
    if "valid_to" in row and row["valid_to"]:
        valid_to = dateutil.parser.isoparse(
            row["valid_to"])-timedelta(seconds=1)

    cfg_timezone = pytz.timezone(cfg['timezone'])
    to_nextday_dt = valid_to + timedelta(days=1)
    points = []
    cur_dt = valid_from
    while cur_dt < to_nextday_dt:
        if cur_dt >= from_dt - timedelta(days=1):
            points.append(rate2point(cur_dt))

        next_local_date = (
            cur_dt.astimezone(cfg_timezone).date() + timedelta(days=1)
        )
        cur_dt = cfg_timezone.localize(
            datetime.combine(next_local_date, datetime.min.time())
        )

        if cur_dt > valid_to:
            points.append(rate2point(valid_to))
            break

    return points


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
    return Point(measurement) \
        .tag("energy_type", usage.energy_type)\
        .tag("direction", usage.direction)\
        .tag("meter_point", usage.meter_point)\
        .tag("meter_serial", usage.meter_serial)\
        .field("interval_start", interval_start.timestamp())\
        .field("interval_end", interval_end.timestamp())\
        .field(usage.unit, row["consumption"])\
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


def query_last_datetime(client: InfluxDBClient3,
                        sql: str, from_max_days_ago: int) -> datetime:
    """Return the timestamp of the most recent point matching the SQL query.

    The SQL query must select a single column aliased `last_time` (e.g.
    MAX("time")). The function will look for data at most from_max_days_ago old.
    If no matching row is found, it returns the timestamp from
    from_max_days_ago. Query failures are surfaced to the caller.
    """
    result = client.query(query=sql, language="sql")
    table = _read_query_result(result)
    last_dt = table.column("last_time")[0].as_py() if table.num_rows else None
    if last_dt is None:
        return datetime_from_days_ago(from_max_days_ago)
    # InfluxDB returns UTC timestamps; make sure they are timezone-aware:
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return last_dt


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
          AND "time" >= now() - INTERVAL '{from_max_days_ago} days'
    '''
    return query_last_datetime(client, sql, from_max_days_ago)


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
          AND "time" >= now() - INTERVAL '{from_max_days_ago} days'
    '''
    last_dt = query_last_datetime(client, sql, from_max_days_ago)
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
        'influx_write_batch_size',
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


def sync_data(client: InfluxDBClient3,
              http_session: requests.Session) -> None:
    """Retrieve configured Octopus data and write it to InfluxDB."""
    to_dt = datetime_to_days_ago(cfg['to_days_ago'])
    to_iso8601 = iso8601_from_datetime(to_dt)
    try:
        from_days_ago = cfg['from_days_ago']
        configured_from_dt = datetime_from_days_ago(from_days_ago)
        configured_from_iso8601 = iso8601_from_datetime(configured_from_dt)
        logging.info(
            f'`from_days_ago` is defined: retrieving from '
            f'{from_days_ago} days ago.')
    except confuse.exceptions.NotFoundError:
        from_days_ago = None
        configured_from_dt = None
        configured_from_iso8601 = None

    existing_measurements = list_influx_measurements(client)
    usage_measurement = cfg['influx_usage_measurement']
    tariff_measurement = cfg['influx_tariff_measurement']

    logging.info('=== Retrieving consumption...')
    for usage in cfg['usage']:
        consumption_url = get_url_of_consumption(cfg['base_url'], usage)
        logging.debug(f'API URL: {consumption_url}')

        if configured_from_iso8601 is not None:
            from_iso8601 = configured_from_iso8601
        elif usage_measurement in existing_measurements:
            from_iso8601 = consumption_last_iso8601(
                client,
                cfg['from_max_days_ago'],
                usage_measurement,
                usage.direction,
                usage.meter_point,
                usage.meter_serial,
            )
        else:
            from_iso8601 = iso8601_from_datetime(
                datetime_from_days_ago(cfg['from_max_days_ago'])
            )

        logging.info(
            f'====== Retrieving {usage.energy_type} {usage.direction} '
            f'({usage.meter_point}) from Octopus...')
        logging.debug(f'from {from_iso8601} to {to_iso8601}')
        data = retrieve_paginated_data(
            cfg['octopus_api_key'],
            consumption_url,
            from_iso8601,
            to_iso8601,
            session=http_session,
            timeout_seconds=cfg['request_timeout_seconds'],
        )

        logging.info(
            f'       ... {len(data)} points retrieved from Octopus.')
        logging.info(
            f'====== Writing {usage.energy_type} {usage.direction} '
            f'({usage.meter_point}) to Influx...')

        # Octopus returns newest first. Writing oldest first ensures an
        # interrupted backfill resumes from the latest successfully stored row.
        points = [
            consumption_to_point(usage_measurement, row, usage)
            for row in reversed(data)
        ]
        if cfg['loglevel'] == 'DEBUG':
            logging.debug(
                '\n' + '\n'.join(p.to_line_protocol() for p in points)
            )
        write_points(client, points, cfg['influx_write_batch_size'])
        if points:
            existing_measurements.add(usage_measurement)
        logging.info(
            f'       ... {len(points)} points written to Influx.')

    logging.info('=== Retrieving tariffs...')
    for tariff in cfg['tariffs']:
        for price_type, unit in cfg['price_types'].items():
            url = get_url_of_tariff(cfg['base_url'], tariff, price_type)

            if configured_from_dt is not None:
                from_dt = configured_from_dt
                from_iso8601 = configured_from_iso8601
            elif tariff_measurement in existing_measurements:
                from_dt = tariff_last_datetime(
                    client,
                    cfg['from_max_days_ago'],
                    tariff_measurement,
                    tariff.energy_type,
                    price_type,
                    tariff.tariff_code,
                )
                from_iso8601 = iso8601_from_datetime(from_dt)
            else:
                from_dt = datetime_from_days_ago(
                    cfg['from_max_days_ago']
                )
                from_iso8601 = iso8601_from_datetime(from_dt)

            logging.info(
                f'====== Retrieving {tariff.energy_type} {price_type} '
                f'price of tariff {tariff.full_name} from Octopus...')
            logging.debug(f'from {from_iso8601} to {to_iso8601}')
            data = retrieve_paginated_data(
                cfg['octopus_api_key'],
                url,
                from_iso8601,
                to_iso8601,
                session=http_session,
                timeout_seconds=cfg['request_timeout_seconds'],
            )
            if cfg['loglevel'] == 'DEBUG':
                logging.debug(
                    '\n' + '\n'.join(str(point) for point in data)
                )
            logging.info(
                f'       ... {len(data)} points retrieved from Octopus.')
            logging.info(
                f'====== Writing {tariff.energy_type} {price_type} '
                f'price of tariff {tariff.full_name} to Influx...')
            logging.debug(f'from {from_dt} to {to_dt}')

            points = []
            for row in reversed(data):
                points.extend(std_unit_rate_to_points(
                    tariff_measurement,
                    row,
                    price_type,
                    unit,
                    tariff,
                    from_dt,
                    to_dt,
                ))

            if cfg['loglevel'] == 'DEBUG':
                logging.debug(
                    '\n' + '\n'.join(p.to_line_protocol() for p in points)
                )
            write_points(client, points, cfg['influx_write_batch_size'])
            if points:
                existing_measurements.add(tariff_measurement)
            logging.info(
                f'       ... {len(points)} points written to Influx '
                '(including any extra points for easier querying and '
                'better charting).')


def main() -> None:
    """Load configuration and run one synchronization."""
    # Confuse automatically tries to load config.yaml from a number of
    # locations. Also try to load a config file in the same directory:
    local_config_path = path.join(path.realpath(
        path.dirname(__file__)), confuse.CONFIG_FILENAME)
    read_local_config = False
    try:
        cfg.set_file(local_config_path)
        read_local_config = True
    except confuse.exceptions.ConfigReadError:
        pass

    parser = build_argparser(params)
    args = parser.parse_args()
    cfg.set_args(args)
    cfg.set_env()
    validate_configuration(parser)
    logging.root.setLevel(cfg['loglevel'])

    if read_local_config:
        logging.info(f'Read configuration from {local_config_path}.')

    with create_http_session(cfg['request_max_retries']) as http_session:
        with InfluxDBClient3(
                host=cfg['influx_url'],
                token=cfg['influx_api_token'],
                database=cfg['influx_database']) as client:
            sync_data(client, http_session)


if __name__ == "__main__":
    main()
