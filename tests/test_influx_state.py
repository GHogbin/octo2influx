from datetime import datetime, timezone
from unittest.mock import Mock

from influxdb_client_3 import Point

from octo2influx_core.influx import (
    make_stream_id,
    query_watermark,
    watermark_point,
    write_records,
)


class FakeValue:
    def __init__(self, value):
        self.value = value

    def as_py(self):
        return self.value


class FakeColumn:
    def __init__(self, values):
        self.values = values

    def __getitem__(self, index):
        return FakeValue(self.values[index])


class FakeTable:
    def __init__(self, values):
        self.values = values
        self.num_rows = len(values)

    def column(self, _name):
        return FakeColumn(self.values)


def test_stream_ids_are_stable_and_do_not_expose_source_values():
    first = make_stream_id('usage', 'electricity', 'secret-meter')
    second = make_stream_id('usage', 'electricity', 'secret-meter')

    assert first == second
    assert 'secret-meter' not in first


def test_query_watermark_returns_aware_utc_timestamp():
    client = Mock()
    client.query.return_value = FakeTable([
        datetime(2024, 1, 1, 12),
    ])

    actual = query_watermark(
        client,
        'octopus-watermarks',
        'usage-123',
    )

    assert actual == datetime(
        2024, 1, 1, 12, tzinfo=timezone.utc)


def test_write_records_commits_watermark_with_final_batch():
    client = Mock()
    records = [
        Point('usage').field('value', value)
        for value in range(5)
    ]
    checkpoint = watermark_point(
        'watermarks',
        'usage-123',
        'usage',
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        rows_written=5,
    )

    write_records(client, records, batch_size=2, watermark=checkpoint)

    batches = [
        call.kwargs['record'] for call in client.write.call_args_list
    ]
    assert [len(batch) for batch in batches] == [2, 2, 2]
    assert batches[-1][-1] is checkpoint
