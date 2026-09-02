from datetime import datetime, timezone

from octo2influx_core.dispatches import (
    CompletedDispatch,
    DispatchBook,
    completed_dispatch_point,
    dispatch_poll_point,
    dispatch_measurement_metadata_point,
    query_completed_dispatches,
)


def dispatch(
        start='2026-09-01T12:00:00Z',
        end='2026-09-01T12:30:00Z',
        source='smart-charge',
):
    return CompletedDispatch.from_graphql({
        'start': start,
        'end': end,
        'delta': '-0.58',
        'meta': {
            'source': source,
            'location': 'AT_HOME',
        },
    })


def test_completed_dispatch_requires_offset_timestamps():
    try:
        dispatch(start='2026-09-01T12:00:00')
    except ValueError as error:
        assert 'offsets' in str(error)
    else:
        raise AssertionError('Expected offset validation to fail.')


def test_dispatch_book_deduplicates_and_sorts_dispatches():
    first = dispatch()
    later = dispatch(
        start='2026-09-01T13:00:00Z',
        end='2026-09-01T13:30:00Z',
    )

    book = DispatchBook.from_dispatches([later, first, first])

    assert book.completed == (first, later)


def test_ten_minutes_of_smart_charge_qualifies_half_hour():
    book = DispatchBook.from_dispatches([
        dispatch(
            start='2026-09-01T12:10:00Z',
            end='2026-09-01T12:20:00Z',
        ),
    ])

    assert book.qualifies_for_cheap_rate(
        datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
        datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc),
    )


def test_less_than_ten_minutes_does_not_qualify():
    book = DispatchBook.from_dispatches([
        dispatch(
            start='2026-09-01T12:10:00Z',
            end='2026-09-01T12:19:59Z',
        ),
    ])

    assert not book.qualifies_for_cheap_rate(
        datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
        datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc),
    )


def test_bump_charge_is_stored_but_not_pricing_eligible():
    item = dispatch(source='bump-charge')
    book = DispatchBook.from_dispatches([item])

    assert not book.qualifies_for_cheap_rate(
        datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
        datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc),
    )
    line = completed_dispatch_point(
        'octopus-dispatches', item, 'account-hash').to_line_protocol()
    assert 'source="bump-charge"' in line
    assert 'pricing_eligible=false' in line
    assert 'delta="-0.58"' in line


def test_completed_dispatch_point_uses_stable_hashed_identity():
    item = dispatch()

    first = completed_dispatch_point(
        'octopus-dispatches', item, 'account-hash').to_line_protocol()
    second = completed_dispatch_point(
        'octopus-dispatches', item, 'account-hash').to_line_protocol()

    assert first == second
    assert 'account_id=account-hash' in first
    assert 'dispatch_id=' in first
    assert '2026-09-01T12:00:00+00:00' in first


def test_dispatch_metadata_defines_optional_table_schema():
    line = dispatch_measurement_metadata_point(
        'octopus-dispatches', False).to_line_protocol()

    assert 'dispatch_type=metadata' in line
    assert 'enabled=false' in line
    assert 'pricing_eligible=false' in line
    assert 'duration_minutes=0' in line
    assert line.endswith(' 0')


class FakeQueryResult:
    def __init__(self, rows):
        self.rows = rows

    def to_pylist(self):
        return self.rows


def test_query_completed_dispatches_reloads_retained_history():
    client = type('Client', (), {})()
    client.query = lambda **_kwargs: FakeQueryResult([{
        'time': datetime(2026, 9, 1, 12),
        'end': '2026-09-01T12:30:00+00:00',
        'delta': '-0.58',
        'source': 'smart-charge',
        'location': 'AT_HOME',
    }])

    result = query_completed_dispatches(
        client,
        'octopus-dispatches',
        'account-hash',
        datetime(2026, 8, 31, tzinfo=timezone.utc),
        datetime(2026, 9, 2, tzinfo=timezone.utc),
    )

    assert result == [dispatch()]


def test_dispatch_poll_uses_opaque_account_id():
    line = dispatch_poll_point(
        'octopus-dispatches',
        'account-hash',
        3,
    ).to_line_protocol()

    assert 'account_id=account-hash' in line
    assert 'records_seen=3i' in line
