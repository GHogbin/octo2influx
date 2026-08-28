from datetime import datetime, timezone
from unittest.mock import Mock

from freezegun import freeze_time
import pytest
import pytz

import octo2influx
from octo2influx import cfg


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeColumn:
    def __init__(self, values):
        self.values = values

    def to_pylist(self):
        return self.values

    def __getitem__(self, index):
        return FakeScalar(self.values[index])


class FakeScalar:
    def __init__(self, value):
        self.value = value

    def as_py(self):
        return self.value


class FakeTable:
    def __init__(self, columns):
        self.columns = columns
        self.num_rows = len(next(iter(columns.values()), []))

    def column(self, name):
        return FakeColumn(self.columns[name])


def test_configuration_validator_rejects_false_result():
    cfg.set({'to_days_ago': -1})

    with pytest.raises(TypeError, match='to_days_ago'):
        cfg['to_days_ago']


def test_argparser_uses_zero_default_config_value():
    cfg.set({'to_days_ago': 7})

    args = octo2influx.build_argparser(octo2influx.params).parse_args([])

    assert args.to_days_ago == 7


def test_date_boundaries_use_account_timezone():
    cfg.set({'timezone': 'Europe/London'})

    with freeze_time('2024-06-01T23:30:00Z'):
        actual = octo2influx.datetime_from_days_ago(0)

    london = pytz.timezone('Europe/London')
    assert actual == london.localize(datetime(2024, 6, 2))


def test_retrieve_paginated_data_follows_next_link():
    session = Mock()
    session.get.side_effect = [
        FakeResponse({
            'results': [{'value': 1}],
            'next': 'https://api.example.test/data?page=2',
        }),
        FakeResponse({
            'results': [{'value': 2}],
            'next': None,
        }),
    ]

    results = octo2influx.retrieve_paginated_data(
        'secret',
        'https://api.example.test/data',
        '2024-01-01T00:00:00Z',
        '2024-01-02T00:00:00Z',
        session=session,
        timeout_seconds=12,
    )

    assert results == [{'value': 1}, {'value': 2}]
    assert session.get.call_count == 2
    assert session.get.call_args_list[0].kwargs['params'] == {
        'period_from': '2024-01-01T00:00:00Z',
        'period_to': '2024-01-02T00:00:00Z',
    }
    assert session.get.call_args_list[1].args[0].endswith('page=2')
    assert session.get.call_args_list[1].kwargs['params'] is None
    assert session.get.call_args_list[0].kwargs['timeout'] == 12


def test_retrieve_paginated_data_skips_empty_range():
    session = Mock()

    results = octo2influx.retrieve_paginated_data(
        'secret',
        'https://api.example.test/data',
        '2024-01-02T00:00:00Z',
        '2024-01-02T00:00:00Z',
        session=session,
    )

    assert results == []
    session.get.assert_not_called()


def test_retrieve_paginated_data_rejects_malformed_payload():
    session = Mock()
    session.get.return_value = FakeResponse({'next': None})

    with pytest.raises(ValueError, match='results list'):
        octo2influx.retrieve_paginated_data(
            'secret',
            'https://api.example.test/data',
            '2024-01-01T00:00:00Z',
            '2024-01-02T00:00:00Z',
            session=session,
        )


def test_http_session_retries_only_configured_transient_failures():
    session = octo2influx.create_http_session(3)
    try:
        retries = session.get_adapter('https://').max_retries
        assert retries.total == 3
        assert retries.allowed_methods == frozenset({'GET'})
        assert 429 in retries.status_forcelist
        assert 400 not in retries.status_forcelist
    finally:
        session.close()


def test_list_influx_measurements_uses_information_schema():
    client = Mock()
    client.query.return_value = FakeTable({
        'table_name': ['octopus-usage', 'octopus-tariffs'],
    })

    measurements = octo2influx.list_influx_measurements(client)

    assert measurements == {'octopus-usage', 'octopus-tariffs'}
    query = client.query.call_args.kwargs['query']
    assert 'information_schema.tables' in query


def test_query_last_datetime_surfaces_query_failures():
    client = Mock()
    client.query.side_effect = RuntimeError('authentication failed')

    with pytest.raises(RuntimeError, match='authentication failed'):
        octo2influx.query_last_datetime(
            client,
            'SELECT MAX("time") AS last_time FROM "usage"',
            60,
        )


def test_consumption_last_datetime_uses_bounded_recent_windows():
    client = Mock()
    latest = datetime(2026, 8, 27, 22, 30, tzinfo=timezone.utc)
    client.query.side_effect = [
        FakeTable({'last_time': [None]}),
        FakeTable({'last_time': [latest]}),
    ]

    result = octo2influx.consumption_last_iso8601(
        client,
        60,
        'octopus-usage',
        'import',
        'meter-point',
        'meter-serial',
    )

    assert result == '2026-08-27T22:30:00Z'
    assert client.query.call_count == 2
    first_query = client.query.call_args_list[0].kwargs['query']
    second_query = client.query.call_args_list[1].kwargs['query']
    assert '''INTERVAL '6 hours' '''.strip() in first_query
    assert '''INTERVAL '0 hours' '''.strip() in first_query
    assert '''INTERVAL '12 hours' '''.strip() in second_query
    assert '''INTERVAL '6 hours' '''.strip() in second_query
    assert '''INTERVAL '60 days' '''.strip() not in first_query


def test_sql_quoting_handles_configured_names():
    assert octo2influx.quote_sql_identifier('a"b') == '"a""b"'
    assert octo2influx.quote_sql_string("a'b") == "'a''b'"


def test_write_points_batches_large_backfills():
    client = Mock()
    points = list(range(5))

    octo2influx.write_points(client, points, batch_size=2)

    batches = [
        call.kwargs['record'] for call in client.write.call_args_list
    ]
    assert batches == [[0, 1], [2, 3], [4]]


def test_date_range_validation_rejects_inverted_range(
        load_example_config):
    cfg.set({'from_days_ago': 1, 'to_days_ago': 2})
    parser = octo2influx.build_argparser(octo2influx.params)

    with pytest.raises(SystemExit):
        octo2influx.validate_configuration(parser)
