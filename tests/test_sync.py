from datetime import datetime, timezone
from unittest.mock import Mock

from freezegun import freeze_time
import pytest
import requests

import octo2influx
from octo2influx_core.models import TariffConfig, UsageConfig
from octo2influx_core.octopus import ApiPage


def usage(meter_point='mpan', meter_serial='serial'):
    return UsageConfig(
        energy_type='electricity',
        direction='import',
        meter_point=meter_point,
        meter_serial=meter_serial,
        unit='kWh',
    )


def tariff(direction='import'):
    return TariffConfig(
        energy_type='electricity',
        direction=direction,
        product_code='TEST',
        tariff_code='E-1R-TEST-C',
        full_name='Test',
        display_name='Test',
        description='',
    )


def rate_row(value):
    return {
        'value_exc_vat': value,
        'value_inc_vat': value,
        'valid_from': '2023-01-01T00:00:00Z',
        'valid_to': None,
        'payment_method': None,
    }


def consumption_row(start, end, value=1):
    return {
        'consumption': value,
        'interval_start': start,
        'interval_end': end,
    }


class FakeOctopusClient:
    def __init__(self, failed_meter=None):
        self.failed_meter = failed_meter

    def rate_pages(self, _tariff, price_type, _from, _to):
        value = 25 if price_type == 'standard-unit-rates' else 40
        yield ApiPage([rate_row(value)], 1, 1)

    def consumption_pages(self, usage_item, _from, _to):
        if usage_item.meter_point == self.failed_meter:
            raise requests.ConnectionError('meter unavailable')
        yield ApiPage([
            consumption_row(
                '2024-01-01T00:00:00Z',
                '2024-01-01T00:30:00Z',
                2,
            ),
        ], 1, 1)


class MultiPageMissingRateClient(FakeOctopusClient):
    def rate_pages(self, _tariff, price_type, _from, _to):
        value = 25 if price_type == 'standard-unit-rates' else 40
        rows = [rate_row(value)]
        if price_type == 'standard-unit-rates':
            rows = [
                {
                    **rate_row(value),
                    'valid_to': '2024-01-02T00:00:00Z',
                },
                {
                    **rate_row(value),
                    'valid_from': '2024-01-03T00:00:00Z',
                },
            ]
        yield ApiPage(rows, 1, 1)

    def consumption_pages(self, _usage, _from, _to):
        yield ApiPage([
            consumption_row(
                '2024-01-02T00:00:00Z',
                '2024-01-02T00:30:00Z',
            ),
        ], 1, 2)
        yield ApiPage([
            consumption_row(
                '2024-01-03T00:00:00Z',
                '2024-01-03T00:30:00Z',
            ),
        ], 2, 2)


class EmptyExportRateClient(FakeOctopusClient):
    def rate_pages(self, tariff_item, price_type, from_iso, to_iso):
        if tariff_item.direction == 'export':
            return
        yield from super().rate_pages(
            tariff_item, price_type, from_iso, to_iso)


def written_lines(client):
    return [
        point.to_line_protocol()
        for call in client.write.call_args_list
        for point in call.kwargs['record']
    ]


def test_sync_writes_raw_rates_costs_watermarks_and_status(
        load_example_config, monkeypatch):
    monkeypatch.setattr(
        octo2influx, 'list_measurements', lambda _client: set())
    monkeypatch.setattr(
        octo2influx,
        'stored_watermark',
        lambda _client, _measurements, _stream_id: None,
    )
    monkeypatch.setattr(
        octo2influx,
        'tariff_last_datetime',
        lambda _client, _days, _measurement, _energy, _price, _tariff:
            octo2influx.default_from_datetime(),
    )
    client = Mock()

    with freeze_time('2024-01-02T12:00:00Z'):
        octo2influx.sync_data(
            client,
            FakeOctopusClient(),
            [usage()],
            [tariff()],
        )

    lines = written_lines(client)
    assert any(line.startswith('octopus-usage,') for line in lines)
    assert any(
        line.startswith('octopus-costs,cost_type=usage')
        for line in lines
    )
    assert any(
        line.startswith('octopus-costs,cost_type=standing')
        for line in lines
    )
    assert any(line.startswith('octopus-watermarks,') for line in lines)
    assert any(
        line.startswith('octopus-sync-status,status=success')
        for line in lines
    )


def test_failed_usage_stream_does_not_block_other_meters(
        load_example_config, monkeypatch):
    monkeypatch.setattr(
        octo2influx, 'list_measurements', lambda _client: set())
    monkeypatch.setattr(
        octo2influx,
        'stored_watermark',
        lambda _client, _measurements, _stream_id: None,
    )
    monkeypatch.setattr(
        octo2influx,
        'tariff_last_datetime',
        lambda _client, _days, _measurement, _energy, _price, _tariff:
            octo2influx.default_from_datetime(),
    )
    client = Mock()

    with freeze_time('2024-01-02T12:00:00Z'):
        with pytest.raises(octo2influx.SynchronizationError):
            octo2influx.sync_data(
                client,
                FakeOctopusClient(failed_meter='broken'),
                [usage('broken'), usage('working')],
                [tariff()],
            )

    lines = written_lines(client)
    assert any('meter_point=working' in line for line in lines)
    assert any(
        line.startswith('octopus-sync-status,status=failed')
        for line in lines
    )


def test_empty_tariff_window_without_related_usage_advances_checkpoint(
        load_example_config, monkeypatch):
    monkeypatch.setattr(
        octo2influx, 'list_measurements', lambda _client: set())
    monkeypatch.setattr(
        octo2influx,
        'stored_watermark',
        lambda _client, _measurements, _stream_id: None,
    )
    client = Mock()

    with freeze_time('2024-01-02T12:00:00Z'):
        octo2influx.sync_data(
            client,
            EmptyExportRateClient(),
            [usage()],
            [tariff(direction='export')],
        )

    lines = written_lines(client)
    assert any(
        'stream_type=tariff' in line and 'rows_written=0i' in line
        for line in lines
    )
    assert any(
        line.startswith('octopus-sync-status,status=success')
        for line in lines
    )


def test_standing_charge_is_deduplicated_across_meter_replacement(
        load_example_config, monkeypatch):
    monkeypatch.setattr(
        octo2influx, 'list_measurements', lambda _client: set())
    monkeypatch.setattr(
        octo2influx,
        'stored_watermark',
        lambda _client, _measurements, _stream_id: None,
    )
    client = Mock()

    with freeze_time('2024-01-02T12:00:00Z'):
        octo2influx.sync_data(
            client,
            FakeOctopusClient(),
            [
                usage(meter_serial='old-meter'),
                usage(meter_serial='new-meter'),
            ],
            [tariff()],
        )

    standing_lines = [
        line for line in written_lines(client)
        if line.startswith('octopus-costs,cost_type=standing')
    ]
    standing_timestamps = [line.rsplit(' ', 1)[-1] for line in standing_lines]
    assert len(standing_lines) == len(set(standing_timestamps))
    assert len(standing_lines) > 1
    assert all('meter_serial=' not in line for line in standing_lines)


def test_cost_watermark_does_not_leapfrog_failed_page(
        load_example_config, monkeypatch):
    monkeypatch.setattr(
        octo2influx, 'list_measurements', lambda _client: set())
    monkeypatch.setattr(
        octo2influx,
        'stored_watermark',
        lambda _client, _measurements, _stream_id: None,
    )
    client = Mock()

    with freeze_time('2024-01-04T12:00:00Z'):
        with pytest.raises(octo2influx.SynchronizationError):
            octo2influx.sync_data(
                client,
                MultiPageMissingRateClient(),
                [usage()],
                [tariff()],
            )

    lines = written_lines(client)
    assert not any('stream_type=cost-usage' in line for line in lines)
    assert any('stream_type=usage' in line for line in lines)
