from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Iterable

import dateutil.parser
from influxdb_client_3 import InfluxDBClient3, Point


SMART_CHARGE_SOURCE = 'smart-charge'
MINIMUM_CHEAP_OVERLAP = timedelta(minutes=10)


@dataclass(frozen=True)
class CompletedDispatch:
    start: datetime
    end: datetime
    delta: str | None = None
    source: str | None = None
    location: str | None = None

    @classmethod
    def from_graphql(cls, value: dict[str, Any]) -> 'CompletedDispatch':
        if not isinstance(value, dict):
            raise ValueError('Completed dispatch is not an object.')
        start_raw = value.get('start')
        end_raw = value.get('end')
        if not isinstance(start_raw, str) or not isinstance(end_raw, str):
            raise ValueError('Completed dispatch has no start or end timestamp.')
        start = dateutil.parser.isoparse(start_raw)
        end = dateutil.parser.isoparse(end_raw)
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError('Completed dispatch timestamps must include offsets.')
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        if end <= start:
            raise ValueError('Completed dispatch end must be after start.')

        meta = value.get('meta')
        if meta is not None and not isinstance(meta, dict):
            raise ValueError('Completed dispatch meta is not an object.')
        meta = meta or {}
        delta = value.get('delta')
        return cls(
            start=start,
            end=end,
            delta=str(delta) if delta is not None else None,
            source=(
                str(meta['source'])
                if meta.get('source') is not None else None
            ),
            location=(
                str(meta['location'])
                if meta.get('location') is not None else None
            ),
        )

    @property
    def pricing_eligible(self) -> bool:
        return self.source == SMART_CHARGE_SOURCE

    def overlap(self, start: datetime, end: datetime) -> timedelta:
        return max(
            timedelta(0),
            min(self.end, end) - max(self.start, start),
        )

    @property
    def identifier(self) -> str:
        source = '\x1f'.join((
            self.start.isoformat(),
            self.end.isoformat(),
            self.delta or '',
            self.source or '',
            self.location or '',
        ))
        return hashlib.sha256(source.encode('utf-8')).hexdigest()[:24]


@dataclass(frozen=True)
class DispatchBook:
    completed: tuple[CompletedDispatch, ...] = ()

    @classmethod
    def from_dispatches(
            cls,
            dispatches: Iterable[CompletedDispatch],
    ) -> 'DispatchBook':
        unique = {
            dispatch.identifier: dispatch
            for dispatch in dispatches
        }
        return cls(tuple(sorted(
            unique.values(),
            key=lambda dispatch: (dispatch.start, dispatch.end),
        )))

    def qualifies_for_cheap_rate(
            self,
            interval_start: datetime,
            interval_end: datetime,
    ) -> bool:
        return any(
            dispatch.pricing_eligible
            and dispatch.overlap(interval_start, interval_end)
            >= MINIMUM_CHEAP_OVERLAP
            for dispatch in self.completed
        )


def completed_dispatch_point(
        measurement: str,
        dispatch: CompletedDispatch,
        account_id: str,
) -> Point:
    point = (
        Point(measurement)
        .tag('dispatch_type', 'completed')
        .tag('dispatch_id', dispatch.identifier)
        .tag('account_id', account_id)
        .field('start', dispatch.start.isoformat())
        .field('end', dispatch.end.isoformat())
        .field(
            'duration_minutes',
            (dispatch.end - dispatch.start).total_seconds() / 60.0,
        )
        .field('pricing_eligible', dispatch.pricing_eligible)
        .time(dispatch.start)
    )
    if dispatch.delta is not None:
        point.field('delta', dispatch.delta)
    if dispatch.source is not None:
        point.field('source', dispatch.source)
    if dispatch.location is not None:
        point.field('location', dispatch.location)
    return point


def dispatch_measurement_metadata_point(
        measurement: str,
        enabled: bool,
) -> Point:
    return (
        Point(measurement)
        .tag('dispatch_type', 'metadata')
        .tag('dispatch_id', 'measurement-metadata')
        .tag('account_id', 'metadata')
        .field('start', '1970-01-01T00:00:00+00:00')
        .field('end', '1970-01-01T00:00:00+00:00')
        .field('duration_minutes', 0.0)
        .field('delta', '')
        .field('source', '')
        .field('location', '')
        .field('pricing_eligible', False)
        .field('enabled', enabled)
        .time(datetime(1970, 1, 1, tzinfo=timezone.utc))
    )


def opaque_account_identifier(account_number: str) -> str:
    return hashlib.sha256(
        account_number.encode('utf-8')
    ).hexdigest()[:24]


def dispatch_poll_point(
        measurement: str,
        account_id: str,
        records_seen: int,
) -> Point:
    timestamp = datetime.now(timezone.utc)
    return (
        Point(measurement)
        .tag('dispatch_type', 'poll')
        .tag('dispatch_id', 'latest-poll')
        .tag('account_id', account_id)
        .field('start', timestamp.isoformat())
        .field('end', timestamp.isoformat())
        .field('duration_minutes', 0.0)
        .field('delta', '')
        .field('source', '')
        .field('location', '')
        .field('pricing_eligible', False)
        .field('enabled', True)
        .field('records_seen', records_seen)
        .time(timestamp)
    )


def query_completed_dispatches(
        client: InfluxDBClient3,
        measurement: str,
        account_id: str,
        from_dt: datetime,
        to_dt: datetime,
) -> list[CompletedDispatch]:
    escaped_measurement = measurement.replace('"', '""')
    from_value = (
        from_dt.astimezone(timezone.utc)
        .isoformat().replace('+00:00', 'Z')
    )
    to_value = (
        to_dt.astimezone(timezone.utc)
        .isoformat().replace('+00:00', 'Z')
    )
    query = f'''
        SELECT time, "end", "delta", "source", "location"
        FROM "{escaped_measurement}"
        WHERE "dispatch_type" = 'completed'
          AND "account_id" = '{account_id}'
          AND time >= CAST('{from_value}' AS TIMESTAMP)
          AND time < CAST('{to_value}' AS TIMESTAMP)
        ORDER BY time
    '''
    result = client.query(query=query, language='sql')
    table = result.read_all() if hasattr(result, 'read_all') else result
    dispatches = []
    for row in table.to_pylist():
        start = row.get('time')
        if not isinstance(start, datetime):
            raise ValueError(
                'Stored completed dispatch has no timestamp.')
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        dispatches.append(CompletedDispatch.from_graphql({
            'start': start.astimezone(timezone.utc).isoformat(),
            'end': row.get('end'),
            'delta': row.get('delta'),
            'meta': {
                'source': row.get('source'),
                'location': row.get('location'),
            },
        }))
    return dispatches
