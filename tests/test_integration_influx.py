import os

from influxdb_client_3 import InfluxDBClient3
import pytest

import octo2influx
from octo2influx_core.influx import read_query_result
from octo2influx_core.models import TariffConfig, UsageConfig
from octo2influx_core.octopus import ApiPage


INFLUX_URL = os.getenv('TEST_INFLUX_URL')
INFLUX_TOKEN = os.getenv('TEST_INFLUX_TOKEN')
INFLUX_DATABASE = os.getenv('TEST_INFLUX_DATABASE')

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
