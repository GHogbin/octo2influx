import os
import json
from pathlib import Path
from datetime import datetime, timezone

from influxdb_client_3 import InfluxDBClient3, Point
import pytest

import octo2influx
from octo2influx_core.influx import read_query_result
from octo2influx_core.models import TariffConfig, UsageConfig
from octo2influx_core.octopus import ApiPage


INFLUX_URL = os.getenv('TEST_INFLUX_URL')
INFLUX_TOKEN = os.getenv('TEST_INFLUX_TOKEN')
INFLUX_DATABASE = os.getenv('TEST_INFLUX_DATABASE')
ROOT = Path(__file__).resolve().parents[1]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not all((INFLUX_URL, INFLUX_TOKEN, INFLUX_DATABASE)),
        reason='InfluxDB integration environment is not configured.',
    ),
]


class FakeOctopusClient:
    def rate_pages(self, _tariff, price_type, _from, _to):
        value = 25 if price_type == 'standard-unit-rates' else 40
        yield ApiPage([{
            'value_exc_vat': value,
            'value_inc_vat': value,
            'valid_from': '2023-01-01T00:00:00Z',
            'valid_to': None,
            'payment_method': None,
        }], 1, 1)

    def consumption_pages(self, _usage, _from, _to):
        yield ApiPage([{
            'consumption': 2,
            'interval_start': '2024-01-01T00:00:00Z',
            'interval_end': '2024-01-01T00:30:00Z',
        }], 1, 1)


def query_rows(client, sql):
    return read_query_result(
        client.query(query=sql, language='sql')
    ).num_rows


def query_scalar(client, sql, column):
    table = read_query_result(
        client.query(query=sql, language='sql')
    )
    return table.column(column)[0].as_py()


def dashboard_queries():
    for filename in ('dashboard.json', 'historical-dashboard.json'):
        dashboard = json.loads(
            (ROOT / 'grafana' / filename).read_text(encoding='utf-8')
        )
        for panel in dashboard['panels']:
            for query_target in panel.get('targets', []):
                query = query_target.get('rawSql')
                if query:
                    yield filename, panel['title'], query


def render_dashboard_query(query):
    replacements = {
        '${usage_measurement}': 'octopus-usage',
        '${tariffs_measurement}': 'octopus-tariffs',
        '${cost_measurement}': 'octopus-costs',
        '${dispatch_measurement}': 'octopus-dispatches',
        '${dispatch_account}': 'dashboard-account',
        '${cost_model}': 'dispatch-aware-v1',
        '${status_measurement}': 'octopus-sync-status',
        '${electricity_import_tariff}': 'E-1R-DASH-IMPORT-C',
        '${electricity_export_tariff}': 'E-1R-DASH-EXPORT-C',
        '${gas_tariff}': 'G-1R-DASH-GAS-C',
        '${account_timezone}': 'Europe/London',
        '${gas_unit}': 'm3',
        '${chart_interval}': '30 minutes',
    }
    for variable, value in replacements.items():
        query = query.replace(variable, value)

    time_from = "CAST('2024-01-01T00:00:00Z' AS TIMESTAMP)"
    time_to = "CAST('2024-01-03T00:00:00Z' AS TIMESTAMP)"
    query = query.replace(
        '$__timeFilter(time)',
        f'time >= {time_from} AND time <= {time_to}',
    )
    query = query.replace('$__timeFrom', time_from)
    query = query.replace('$__timeTo', time_to)
    return query


def dashboard_fixture_points():
    jan_1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    jan_1_midpoint = datetime(
        2024, 1, 1, 0, 15, tzinfo=timezone.utc)
    jan_2 = datetime(2024, 1, 2, tzinfo=timezone.utc)

    usage_points = [
        Point('octopus-usage')
        .tag('energy_type', 'electricity')
        .tag('direction', 'import')
        .tag('meter_point', 'dashboard-import')
        .tag('meter_serial', 'dashboard-import-serial')
        .field('kWh', 2.0)
        .field('value', 2.0)
        .field('unit', 'kWh')
        .time(jan_1_midpoint),
        Point('octopus-usage')
        .tag('energy_type', 'electricity')
        .tag('direction', 'export')
        .tag('meter_point', 'dashboard-export')
        .tag('meter_serial', 'dashboard-export-serial')
        .field('kWh', 0.5)
        .field('value', 0.5)
        .field('unit', 'kWh')
        .time(jan_1_midpoint),
        Point('octopus-usage')
        .tag('energy_type', 'gas')
        .tag('direction', 'import')
        .tag('meter_point', 'dashboard-gas')
        .tag('meter_serial', 'dashboard-gas-serial')
        .field('m3', 1.0)
        .field('value', 1.0)
        .field('unit', 'm3')
        .time(jan_1_midpoint),
    ]

    tariff_points = []
    for direction, tariff_code, rate in (
            ('import', 'E-1R-DASH-IMPORT-C', 25.0),
            ('export', 'E-1R-DASH-EXPORT-C', 15.0)):
        for timestamp in (jan_1, jan_2):
            tariff_points.append(
                Point('octopus-tariffs')
                .tag('energy_type', 'electricity')
                .tag('direction', direction)
                .tag('tariff_code', tariff_code)
                .tag('price_type', 'standard-unit-rates')
                .tag('product_code', 'DASH')
                .tag('display_name', f'Dashboard {direction}')
                .field('p/kWh_inc_vat', rate)
                .field('value_inc_vat', rate)
                .field('unit', 'p/kWh')
                .time(timestamp)
            )
        tariff_points.append(
            Point('octopus-tariffs')
            .tag('energy_type', 'electricity')
            .tag('direction', direction)
            .tag('tariff_code', tariff_code)
            .tag('price_type', 'standing-charges')
            .tag('product_code', 'DASH')
            .tag('display_name', f'Dashboard {direction}')
            .field('p/day_inc_vat', 40.0 if direction == 'import' else 0.0)
            .field('value_inc_vat', 40.0 if direction == 'import' else 0.0)
            .field('unit', 'p/day')
            .time(jan_1)
        )

    tariff_points.append(
        Point('octopus-tariffs')
        .tag('energy_type', 'gas')
        .tag('direction', 'import')
        .tag('tariff_code', 'G-1R-DASH-GAS-C')
        .tag('price_type', 'standing-charges')
        .tag('product_code', 'DASH-GAS')
        .tag('display_name', 'Dashboard gas')
        .field('p/day_inc_vat', 30.0)
        .field('value_inc_vat', 30.0)
        .field('unit', 'p/day')
        .time(jan_1)
    )

    cost_points = [
        Point('octopus-costs')
        .tag('cost_type', 'usage')
        .tag('cost_model', 'dispatch-aware-v1')
        .tag('energy_type', 'electricity')
        .tag('direction', 'import')
        .tag('meter_point', 'dashboard-import')
        .tag('meter_serial', 'dashboard-import-serial')
        .tag('tariff_code', 'E-1R-DASH-IMPORT-C')
        .tag('price_type', 'standard-unit-rates')
        .field('value_gbp', 0.5)
        .time(jan_1_midpoint),
        Point('octopus-costs')
        .tag('cost_type', 'standing')
        .tag('cost_model', 'dispatch-aware-v1')
        .tag('energy_type', 'electricity')
        .tag('direction', 'import')
        .tag('meter_point', 'dashboard-import')
        .tag('tariff_code', 'E-1R-DASH-IMPORT-C')
        .tag('price_type', 'standing-charges')
        .field('value_gbp', 0.4)
        .time(jan_1),
        Point('octopus-costs')
        .tag('cost_type', 'usage')
        .tag('cost_model', 'dispatch-aware-v1')
        .tag('energy_type', 'electricity')
        .tag('direction', 'export')
        .tag('meter_point', 'dashboard-export')
        .tag('meter_serial', 'dashboard-export-serial')
        .tag('tariff_code', 'E-1R-DASH-EXPORT-C')
        .tag('price_type', 'standard-unit-rates')
        .field('value_gbp', 0.075)
        .time(jan_1_midpoint),
        Point('octopus-costs')
        .tag('cost_type', 'standing')
        .tag('cost_model', 'dispatch-aware-v1')
        .tag('energy_type', 'electricity')
        .tag('direction', 'export')
        .tag('meter_point', 'dashboard-export')
        .tag('tariff_code', 'E-1R-DASH-EXPORT-C')
        .tag('price_type', 'standing-charges')
        .field('value_gbp', 0.0)
        .time(jan_1),
        Point('octopus-costs')
        .tag('cost_type', 'usage')
        .tag('cost_model', 'dispatch-aware-v1')
        .tag('energy_type', 'gas')
        .tag('direction', 'import')
        .tag('meter_point', 'dashboard-gas')
        .tag('meter_serial', 'dashboard-gas-serial')
        .tag('tariff_code', 'G-1R-DASH-GAS-C')
        .tag('price_type', 'standard-unit-rates')
        .field('value_gbp', 0.6)
        .time(jan_1_midpoint),
        Point('octopus-costs')
        .tag('cost_type', 'standing')
        .tag('cost_model', 'dispatch-aware-v1')
        .tag('energy_type', 'gas')
        .tag('direction', 'import')
        .tag('meter_point', 'dashboard-gas')
        .tag('tariff_code', 'G-1R-DASH-GAS-C')
        .tag('price_type', 'standing-charges')
        .field('value_gbp', 0.3)
        .time(jan_1),
    ]

    status_point = (
        Point('octopus-sync-status')
        .tag('status', 'success')
        .field('successful_streams', 9)
        .field('failed_streams', 0)
        .field('duration_seconds', 1.2)
        .time(jan_2)
    )
    dispatch_point = (
        Point('octopus-dispatches')
        .tag('dispatch_type', 'completed')
        .tag('dispatch_id', 'dashboard-dispatch')
        .tag('account_id', 'dashboard-account')
        .field('end', '2024-01-01T01:00:00+00:00')
        .field('duration_minutes', 30.0)
        .field('source', 'smart-charge')
        .field('location', 'AT_HOME')
        .field('pricing_eligible', True)
        .time(jan_1_midpoint)
    )
    dispatch_poll = (
        Point('octopus-dispatches')
        .tag('dispatch_type', 'poll')
        .tag('dispatch_id', 'latest-poll')
        .tag('account_id', 'dashboard-account')
        .field('records_seen', 1)
        .time(jan_2)
    )
    status_point.field('cost_model', 'dispatch-aware-v1')
    return [
        *usage_points,
        *tariff_points,
        *cost_points,
        dispatch_point,
        dispatch_poll,
        status_point,
    ]


def test_full_sync_is_idempotent_against_influxdb3(
        load_example_config):
    octo2influx.cfg.set({
        'from_days_ago': 2,
        'to_days_ago': 1,
    })
    usage = UsageConfig(
        energy_type='electricity',
        direction='import',
        meter_point='integration-mpan',
        meter_serial='integration-serial',
        unit='kWh',
    )
    tariff = TariffConfig(
        energy_type='electricity',
        direction='import',
        product_code='INTEGRATION',
        tariff_code='E-1R-INTEGRATION-C',
        full_name='Integration tariff',
        display_name='Integration tariff',
        description='',
    )

    with InfluxDBClient3(
            host=INFLUX_URL,
            token=INFLUX_TOKEN,
            database=INFLUX_DATABASE) as client:
        octo2influx.sync_data(
            client,
            FakeOctopusClient(),
            [usage],
            [tariff],
        )
        first_usage_count = query_rows(
            client,
            'SELECT * FROM "octopus-usage" '
            "WHERE \"meter_point\" = 'integration-mpan'",
        )
        first_cost_count = query_rows(
            client,
            'SELECT * FROM "octopus-costs" '
            "WHERE \"cost_type\" = 'usage'",
        )

        octo2influx.sync_data(
            client,
            FakeOctopusClient(),
            [usage],
            [tariff],
        )
        second_usage_count = query_rows(
            client,
            'SELECT * FROM "octopus-usage" '
            "WHERE \"meter_point\" = 'integration-mpan'",
        )
        second_cost_count = query_rows(
            client,
            'SELECT * FROM "octopus-costs" '
            "WHERE \"cost_type\" = 'usage'",
        )
        usage_cost = query_scalar(
            client,
            'SELECT SUM("value_gbp") AS total '
            'FROM "octopus-costs" '
            "WHERE \"cost_type\" = 'usage'",
            'total',
        )

    assert first_usage_count == second_usage_count == 1
    assert first_cost_count == second_cost_count == 1
    assert usage_cost == pytest.approx(0.5)


def test_all_dashboard_panel_queries_execute_on_influxdb3():
    with InfluxDBClient3(
            host=INFLUX_URL,
            token=INFLUX_TOKEN,
            database=INFLUX_DATABASE) as client:
        client.write(record=dashboard_fixture_points())
        failures = []
        for filename, title, query in dashboard_queries():
            try:
                client.query(
                    query=render_dashboard_query(query),
                    language='sql',
                )
            except Exception as error:
                failures.append(
                    f'{filename} / {title}: {error}')

    assert not failures, '\n'.join(failures)
