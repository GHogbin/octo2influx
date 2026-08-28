from datetime import datetime, timezone
import hashlib
from typing import Any

from influxdb_client_3 import InfluxDBClient3, Point


def quote_identifier(identifier: str) -> str:
    if not identifier:
        raise ValueError('SQL identifiers cannot be empty.')
    return '"' + identifier.replace('"', '""') + '"'


def quote_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def read_query_result(result):
    return result.read_all() if hasattr(result, 'read_all') else result


def list_measurements(client: InfluxDBClient3) -> set[str]:
    result = client.query(
        query='''
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'iox'
        ''',
        language='sql',
    )
    table = read_query_result(result)
    if not table.num_rows:
        return set()
    return set(table.column('table_name').to_pylist())


def make_stream_id(stream_type: str, *parts: str) -> str:
    source = '\x1f'.join((stream_type, *parts))
    digest = hashlib.sha256(source.encode('utf-8')).hexdigest()[:24]
    return f'{stream_type}-{digest}'


def query_watermark(
        client: InfluxDBClient3,
        measurement: str,
        stream_id: str,
) -> datetime | None:
    sql = f'''
        SELECT MAX("time") AS last_time
        FROM {quote_identifier(measurement)}
        WHERE "stream_id" = {quote_string(stream_id)}
    '''
    result = client.query(query=sql, language='sql')
    table = read_query_result(result)
    value = table.column('last_time')[0].as_py() if table.num_rows else None
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def watermark_point(
        measurement: str,
        stream_id: str,
        stream_type: str,
        source_time: datetime,
        rows_written: int,
) -> Point:
    ingested_at = datetime.now(timezone.utc)
    return (
        Point(measurement)
        .tag('stream_id', stream_id)
        .tag('stream_type', stream_type)
        .field('source_time', source_time.isoformat())
        .field('ingested_at', ingested_at.isoformat())
        .field('rows_written', rows_written)
        .time(source_time)
    )


def write_records(
        client: InfluxDBClient3,
        records: list[Point],
        batch_size: int,
        watermark: Point | None = None,
) -> None:
    if batch_size <= 0:
        raise ValueError('batch_size must be positive.')

    if not records:
        if watermark is not None:
            client.write(record=[watermark])
        return

    for start in range(0, len(records), batch_size):
        batch = records[start:start + batch_size]
        is_last = start + batch_size >= len(records)
        if is_last and watermark is not None:
            batch = [*batch, watermark]
        client.write(record=batch)


def sync_status_point(
        measurement: str,
        successful_streams: int,
        failed_streams: int,
        duration_seconds: float,
        error_summary: str = '',
) -> Point:
    status = 'success' if failed_streams == 0 else 'failed'
    point = (
        Point(measurement)
        .tag('status', status)
        .field('successful_streams', successful_streams)
        .field('failed_streams', failed_streams)
        .field('duration_seconds', duration_seconds)
    )
    if error_summary:
        point.field('error_summary', error_summary[:4096])
    return point.time(datetime.now(timezone.utc))


def scalar_value(table: Any, column: str):
    if not table.num_rows:
        return None
    return table.column(column)[0].as_py()
