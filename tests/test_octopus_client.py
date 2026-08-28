from unittest.mock import Mock

import pytest

from octo2influx_core.models import (
    DAY_UNIT_RATE,
    NIGHT_UNIT_RATE,
    STANDING_CHARGE,
    TariffConfig,
    UsageConfig,
)
from octo2influx_core.octopus import (
    OctopusClient,
    discover_account_configuration,
    product_code_from_tariff_code,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def client_with_session(session, max_pages=1000):
    return OctopusClient(
        session,
        'https://api.example.test/v1',
        'secret',
        timeout_seconds=12,
        max_pages=max_pages,
    )


def usage():
    return UsageConfig(
        energy_type='electricity',
        direction='import',
        meter_point='mpan',
        meter_serial='serial',
        unit='kWh',
    )


def tariff():
    return TariffConfig(
        energy_type='electricity',
        direction='import',
        product_code='TEST',
        tariff_code='E-1R-TEST-C',
        full_name='Test',
        display_name='Test',
        description='',
    )


def test_consumption_pages_are_oldest_first_and_authenticated():
    session = Mock()
    session.get.return_value = FakeResponse({
        'count': 0,
        'next': None,
        'results': [],
    })

    list(client_with_session(session).consumption_pages(
        usage(),
        '2024-01-01T00:00:00Z',
        '2024-01-02T00:00:00Z',
    ))

    kwargs = session.get.call_args.kwargs
    assert kwargs['auth'] == ('secret', '')
    assert kwargs['params']['order_by'] == 'period'
    assert kwargs['params']['page_size'] == 25000


def test_tariff_pages_do_not_send_account_api_key():
    session = Mock()
    session.get.return_value = FakeResponse({
        'count': 0,
        'next': None,
        'results': [],
    })

    list(client_with_session(session).rate_pages(
        tariff(),
        'standard-unit-rates',
        '2024-01-01T00:00:00Z',
        '2024-01-02T00:00:00Z',
    ))

    assert session.get.call_args.kwargs['auth'] is None
    assert session.get.call_args.kwargs['params']['page_size'] == 1500


def test_pagination_rejects_cross_origin_next_link():
    session = Mock()
    session.get.return_value = FakeResponse({
        'count': 1,
        'next': 'https://attacker.example/steal',
        'results': [{'value': 1}],
    })

    with pytest.raises(ValueError, match='cross-origin'):
        list(client_with_session(session).iter_pages(
            'https://api.example.test/v1/data',
            {},
            authenticated=True,
        ))

    assert session.get.call_count == 1


def test_pagination_detects_cycles():
    session = Mock()
    repeated = 'https://api.example.test/v1/data?page=2'
    session.get.side_effect = [
        FakeResponse({
            'count': 2,
            'next': repeated,
            'results': [{'value': 1}],
        }),
        FakeResponse({
            'count': 2,
            'next': repeated,
            'results': [{'value': 2}],
        }),
    ]

    with pytest.raises(ValueError, match='cycle'):
        list(client_with_session(session).iter_pages(
            'https://api.example.test/v1/data',
            {},
            authenticated=True,
        ))


def test_account_discovery_finds_meter_and_dual_rate_tariff():
    account_payload = {
        'number': 'A-TEST',
        'properties': [{
            'moved_out_at': None,
            'electricity_meter_points': [{
                'mpan': '123',
                'is_export': False,
                'meters': [{'serial_number': 'ABC'}],
                'agreements': [{
                    'tariff_code': 'E-2R-TEST-24-01-01-C',
                    'valid_from': '2024-01-01T00:00:00Z',
                    'valid_to': None,
                }],
            }],
            'gas_meter_points': [],
        }],
    }
    product_payload = {
        'code': 'TEST-24-01-01',
        'full_name': 'Economy 7 Test',
        'display_name': 'Economy 7',
        'description': 'Dual-rate tariff',
        'dual_register_electricity_tariffs': {
            '_C': {
                'direct_debit_monthly': {
                    'code': 'E-2R-TEST-24-01-01-C',
                    'links': [
                        {'href': 'https://api.example.test/v1/day-unit-rates/'},
                        {'href': 'https://api.example.test/v1/night-unit-rates/'},
                        {'href': 'https://api.example.test/v1/standing-charges/'},
                    ],
                },
            },
        },
    }
    session = Mock()

    def get(url, **_kwargs):
        if '/accounts/' in url:
            return FakeResponse(account_payload)
        if '/products/' in url:
            return FakeResponse(product_payload)
        raise AssertionError(f'Unexpected URL: {url}')

    session.get.side_effect = get
    client = client_with_session(session)

    usage_items, tariff_items = discover_account_configuration(
        client,
        'A-TEST',
        gas_unit='m3',
    )

    assert usage_items == [
        UsageConfig(
            energy_type='electricity',
            direction='import',
            meter_point='123',
            meter_serial='ABC',
            unit='kWh',
            source='account',
        )
    ]
    assert tariff_items[0].product_code == 'TEST-24-01-01'
    assert tariff_items[0].rate_types == (
        DAY_UNIT_RATE,
        NIGHT_UNIT_RATE,
        STANDING_CHARGE,
    )


def test_product_code_is_derived_from_tariff_code():
    assert product_code_from_tariff_code(
        'E-1R-AGILE-24-10-01-C'
    ) == 'AGILE-24-10-01'
