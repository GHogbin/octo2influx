from unittest.mock import Mock

import pytest
import requests

from octo2influx_core.models import (
    DAY_UNIT_RATE,
    NIGHT_UNIT_RATE,
    STANDING_CHARGE,
    TariffConfig,
    UsageConfig,
)
from octo2influx_core.octopus import (
    OctopusClient,
    OctopusGraphQLClient,
    discover_account_configuration,
    product_code_from_tariff_code,
)


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f'HTTP {self.status_code}', response=self)

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


def test_graphql_uses_api_key_token_and_raw_authorization_header():
    session = Mock()
    session.post.side_effect = [
        FakeResponse({
            'data': {
                'obtainKrakenToken': {'token': 'jwt-token'},
            },
        }),
        FakeResponse({
            'data': {
                'completedDispatches': [{
                    'start': '2026-09-01T12:00:00Z',
                    'end': '2026-09-01T12:30:00Z',
                    'delta': '-0.58',
                    'meta': {
                        'source': 'smart-charge',
                        'location': 'AT_HOME',
                    },
                }],
            },
        }),
    ]
    client = OctopusGraphQLClient(
        session,
        'api-key',
        timeout_seconds=12,
        url='https://api.example.test/v1/graphql/',
    )

    token = client.obtain_token()
    dispatches = client.completed_dispatches(token, 'A-TEST')

    assert token == 'jwt-token'
    assert len(dispatches) == 1
    first_call = session.post.call_args_list[0]
    second_call = session.post.call_args_list[1]
    assert 'Authorization' not in first_call.kwargs['headers']
    assert first_call.kwargs['json']['variables'] == {'apiKey': 'api-key'}
    assert second_call.kwargs['headers']['Authorization'] == 'jwt-token'
    assert second_call.kwargs['json']['variables'] == {
        'accountNumber': 'A-TEST',
    }
    assert second_call.kwargs['timeout'] == 12


def test_graphql_discovers_single_account_without_logging_identifier():
    session = Mock()
    session.post.return_value = FakeResponse({
        'data': {
            'viewer': {
                'accounts': [
                    {'number': 'A-TEST'},
                    {'number': 'A-TEST'},
                ],
            },
        },
    })
    client = OctopusGraphQLClient(session, 'api-key', 12)

    assert client.resolve_account_number('token') == 'A-TEST'


def test_graphql_requires_configured_account_when_multiple_are_visible():
    session = Mock()
    session.post.return_value = FakeResponse({
        'data': {
            'viewer': {
                'accounts': [
                    {'number': 'A-FIRST'},
                    {'number': 'A-SECOND'},
                ],
            },
        },
    })
    client = OctopusGraphQLClient(session, 'api-key', 12)

    with pytest.raises(ValueError, match='exposes 2 accounts'):
        client.resolve_account_number('token')

    assert client.resolve_account_number(
        'token', 'A-CONFIGURED') == 'A-CONFIGURED'


def test_graphql_surfaces_errors_from_http_200_response():
    session = Mock()
    session.post.return_value = FakeResponse({
        'data': None,
        'errors': [{'message': 'Unauthorized'}],
    })
    client = OctopusGraphQLClient(session, 'api-key', 12)

    with pytest.raises(ValueError, match='Unauthorized'):
        client.obtain_token()


def test_graphql_retries_transient_post_with_retry_after():
    session = Mock()
    session.post.side_effect = [
        FakeResponse(
            {'errors': [{'message': 'busy'}]},
            status_code=503,
            headers={'Retry-After': '2'},
        ),
        FakeResponse({
            'data': {
                'obtainKrakenToken': {'token': 'jwt-token'},
            },
        }),
    ]
    sleeper = Mock()
    client = OctopusGraphQLClient(
        session,
        'api-key',
        12,
        max_retries=1,
        sleep_fn=sleeper,
    )

    assert client.obtain_token() == 'jwt-token'
    assert session.post.call_count == 2
    sleeper.assert_called_once_with(2.0)


def test_graphql_retries_rate_limit_error_in_http_200():
    session = Mock()
    session.post.side_effect = [
        FakeResponse({
            'data': None,
            'errors': [{
                'message': 'Rate limited',
                'extensions': {'errorCode': 'KT-CT-1199'},
            }],
        }),
        FakeResponse({
            'data': {
                'viewer': {'accounts': [{'number': 'A-TEST'}]},
            },
        }),
    ]
    sleeper = Mock()
    client = OctopusGraphQLClient(
        session,
        'api-key',
        12,
        max_retries=1,
        sleep_fn=sleeper,
    )

    assert client.account_numbers('token') == ['A-TEST']
    assert session.post.call_count == 2
    assert sleeper.call_count == 1
