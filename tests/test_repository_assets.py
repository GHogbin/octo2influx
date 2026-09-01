import json
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATHS = [
    ROOT / 'grafana' / 'dashboard.json',
    ROOT / 'grafana' / 'historical-dashboard.json',
]


def dashboard_queries(dashboard):
    for panel in dashboard['panels']:
        for target in panel.get('targets', []):
            if 'rawSql' in target:
                yield target['rawSql']


def test_example_config_contains_only_placeholder_meter_ids():
    config_path = ROOT / 'src' / 'config.example.yaml'
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))

    for usage in config['usage']:
        assert usage['meter_point'].startswith('YOUR_')
        assert usage['meter_serial'].startswith('YOUR_')

    config_text = config_path.read_text(encoding='utf-8')
    assert not re.search(r'meter_(?:point|serial):\s*"\d{8,}"', config_text)


def test_compose_defaults_are_local_and_authenticated():
    compose = yaml.safe_load(
        (ROOT / 'docker-compose.example.yml').read_text(encoding='utf-8')
    )
    services = compose['services']

    assert services['influx']['image'].endswith('3.11.2-core}')
    assert services['influx']['command'][0] == 'serve'
    assert 'influxdb3' not in services['influx']['command']
    assert any(
        str(value).startswith('--query-file-limit=')
        for value in services['influx']['command']
    )
    assert services['grafana']['image'].endswith('13.0.7}')
    assert services['influx']['ports'][0].startswith('127.0.0.1:')
    assert services['grafana']['ports'][0].startswith('127.0.0.1:')
    assert services['influx']['healthcheck']['retries'] == 30
    assert 'GF_AUTH_ANONYMOUS_ENABLED=false' in (
        services['grafana']['environment']
    )
    assert any(
        value.startswith(
            'INFLUXDB_TOKEN=${INFLUXDB_TOKEN:?')
        for value in services['grafana']['environment']
    )
    assert services['grafana']['depends_on']['influx']['condition'] == (
        'service_healthy'
    )
    grafana_volumes = services['grafana']['volumes']
    assert any('dashboard.json:' in value for value in grafana_volumes)
    assert any('historical-dashboard.json:' in value
               for value in grafana_volumes)
    assert services['octo2influx']['volumes'][0].endswith(':ro')
    assert services['octo2influx']['environment']['RETRY_FREQ'] == '5m'
    assert services['octo2influx']['depends_on']['influx']['condition'] == (
        'service_healthy'
    )


def test_generated_dashboards_are_current():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / 'grafana' / 'generate_dashboards.py'),
            '--check',
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('dashboard_path', DASHBOARD_PATHS)
def test_dashboard_is_portable_and_uses_safe_time_queries(
        dashboard_path):
    dashboard_text = dashboard_path.read_text(encoding='utf-8')
    dashboard = json.loads(dashboard_text)
    queries = list(dashboard_queries(dashboard))

    assert dashboard['id'] is None
    assert dashboard['uid']
    assert 'c8229740-0e9a-4f46-a107-27c0a34b86fb' not in dashboard_text
    assert '${DS_INFLUXDB}' not in dashboard_text
    assert dashboard_text.count('${datasource}') > 0
    assert 'avg(\\"kWh\\")' not in dashboard_text
    assert '/ 48.0' not in dashboard_text

    cost_queries = [
        query for query in queries
        if 'FROM "${cost_measurement}"' in query
    ]
    assert cost_queries
    assert any('"cost_type"' in query for query in cost_queries)

    daily_queries = [
        query for query in queries
        if "INTERVAL '1 day'" in query
    ]
    assert daily_queries
    for query in daily_queries:
        assert 'date_bin_wallclock' in query
        assert "tz(time, '${account_timezone}')" in query

    variables = {
        item['name']: item for item in dashboard['templating']['list']
    }
    assert variables['datasource']['type'] == 'datasource'
    assert variables['cost_measurement']['query'] == 'octopus-costs'
    assert variables['status_measurement']['query'] == 'octopus-sync-status'
    assert dashboard['timepicker']['time_options'] == [
        '24h', '2d', '3d', '7d',
    ]
    tariff_variables = [
        'electricity_import_tariff',
        'gas_tariff',
    ]
    if dashboard['uid'] == 'octo2influx-overview':
        assert 'gas_unit' not in variables
        assert 'electricity_export_tariff' not in variables
    else:
        assert variables['gas_unit']['query'] == 'm3,kWh'
        tariff_variables.append('electricity_export_tariff')
    for name in tariff_variables:
        assert "time >= now() - INTERVAL '24 hours'" in (
            variables[name]['query']
        )
    assert variables['electricity_import_tariff']['current']['value'] == (
        'E-1R-INTELLI-FIX-12M-26-06-13-H'
    )
    assert variables['gas_tariff']['current']['value'] == (
        'G-1R-VAR-22-11-01-H'
    )
    assert any(panel['title'] == 'Latest Synchronization'
               for panel in dashboard['panels'])

    rate_queries = [
        query for query in queries
        if (
            'date_bin_gapfill' in query
            and (
                'p/kWh_inc_vat' in query
                or '"unit_rate_pence"' in query
            )
        )
    ]
    assert len(rate_queries) == 2
    assert all('date_bin_gapfill' in query for query in rate_queries)
    assert all('locf(' in query for query in rate_queries)

    hourly_queries = [
        query for query in queries
        if 'AS "Hour"' in query and 'AVG(' in query
    ]
    assert hourly_queries
    assert all(
        "date_bin(INTERVAL '1 hour', time)" in query
        for query in hourly_queries
    )
    assert all(
        "interval_time >= $__timeFrom" in query
        and "interval_time + INTERVAL '1 hour' <= $__timeTo" in query
        for query in hourly_queries
    )


def test_overview_matches_reference_information_hierarchy():
    dashboard = json.loads(
        DASHBOARD_PATHS[0].read_text(encoding='utf-8')
    )
    titles = {panel['title'] for panel in dashboard['panels']}
    stat_panels = [
        panel for panel in dashboard['panels']
        if panel['type'] == 'stat' and panel['gridPos']['y'] == 1
    ]
    variables = {
        item['name']: item for item in dashboard['templating']['list']
    }

    assert dashboard['time']['from'] == 'now-3d'
    assert len(stat_panels) == 6
    assert {
        'Electricity Imported',
        'Electricity Usage Cost',
        'Electricity Standing Charge',
        'Electricity Total Cost',
        'Gas Used',
        'Gas Total Cost',
    }.issubset(titles)
    assert {
        'Electricity Usage Cost by Interval',
        'Electricity and Gas Unit Rates',
        'Daily Electricity Import',
        'Gas: Daily kWh and Tariff Rate',
        'Electricity and Gas Tariffs',
        'Electricity Import by Hour of Day',
        'Cumulative Electricity Import',
    }.issubset(titles)
    assert not {
        'Grid Exported',
        'Export Revenue',
    }.intersection(titles)
    daily_cost_panel = next(
        panel for panel in dashboard['panels']
        if panel['title'] == 'Electricity Usage Cost by Interval'
    )
    daily_cost_query = daily_cost_panel['targets'][0]['rawSql']
    assert "date_bin(INTERVAL '${cost_interval}', time)" in (
        daily_cost_query
    )
    assert 'AS "Electricity usage cost"' in daily_cost_query
    assert '"cost_type" = \'usage\'' in daily_cost_query
    assert 'standing' not in daily_cost_query
    assert daily_cost_panel['fieldConfig']['overrides'] == []
    assert variables['cost_interval']['query'] == '30 minutes,1 hour'
    assert variables['cost_interval']['current']['value'] == '30 minutes'
    gas_panel = next(
        panel for panel in dashboard['panels']
        if panel['title'] == 'Gas: Daily kWh and Tariff Rate'
    )
    gas_rate_override = next(
        override for override in gas_panel['fieldConfig']['overrides']
        if override['matcher']['options'] == 'Gas £/kWh'
    )
    override_values = {
        item['id']: item['value']
        for item in gas_rate_override['properties']
    }
    assert override_values['unit'] == 'currencyGBP'
    assert override_values['custom.axisPlacement'] == 'right'
    assert override_values['custom.drawStyle'] == 'line'
    tariff_panel = next(
        panel for panel in dashboard['panels']
        if panel['title'] == 'Electricity and Gas Tariffs'
    )
    for target in tariff_panel['targets']:
        query = target['rawSql']
        assert 'time < $__timeFrom' in query
        assert "time >= $__timeFrom - INTERVAL '2 days'" in query
        assert 'LIMIT 1' in query
        assert 'UNION ALL' in query


@pytest.mark.parametrize('dashboard_path', DASHBOARD_PATHS)
def test_dashboard_panels_do_not_overlap(dashboard_path):
    dashboard = json.loads(dashboard_path.read_text(encoding='utf-8'))
    panels = [
        panel for panel in dashboard['panels']
        if panel['type'] != 'row'
    ]
    for index, first in enumerate(panels):
        for second in panels[index + 1:]:
            a = first['gridPos']
            b = second['gridPos']
            overlaps = not (
                a['x'] + a['w'] <= b['x']
                or b['x'] + b['w'] <= a['x']
                or a['y'] + a['h'] <= b['y']
                or b['y'] + b['h'] <= a['y']
            )
            assert not overlaps, (
                f'{first["title"]!r} overlaps {second["title"]!r}'
            )


def test_historical_dashboard_has_analysis_views():
    dashboard = json.loads(
        DASHBOARD_PATHS[1].read_text(encoding='utf-8')
    )
    panel_types = {panel['type'] for panel in dashboard['panels']}
    titles = {panel['title'] for panel in dashboard['panels']}
    variables = {
        item['name']: item for item in dashboard['templating']['list']
    }

    assert dashboard['time']['from'] == 'now-7d'
    assert variables['HistoryDuration']['query'] == '3d,7d'
    assert 'state-timeline' in panel_types
    assert 'table' in panel_types
    assert {
        'Tariff Timeline',
        'Tariff Comparison',
        'Import by Meter Point',
        'Cumulative Energy Totals',
    }.issubset(titles)
