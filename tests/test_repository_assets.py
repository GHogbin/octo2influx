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
    assert variables['gas_unit']['query'] == 'm3,kWh'
    assert variables['cost_measurement']['query'] == 'octopus-costs'
    assert variables['status_measurement']['query'] == 'octopus-sync-status'
    assert dashboard['timepicker']['time_options'] == [
        '24h', '2d', '3d', '7d',
    ]
    for name in (
            'electricity_import_tariff',
            'electricity_export_tariff',
            'gas_tariff'):
        assert "time >= now() - INTERVAL '24 hours'" in (
            variables[name]['query']
        )
    assert any(panel['title'] == 'Latest Synchronization'
               for panel in dashboard['panels'])

    rate_queries = [
        query for query in queries
        if 'p/kWh_inc_vat' in query
    ]
    assert len(rate_queries) == 2
    assert all('date_bin_gapfill' in query for query in rate_queries)
    assert all('locf(' in query for query in rate_queries)


def test_overview_matches_reference_information_hierarchy():
    dashboard = json.loads(
        DASHBOARD_PATHS[0].read_text(encoding='utf-8')
    )
    titles = {panel['title'] for panel in dashboard['panels']}
    stat_panels = [
        panel for panel in dashboard['panels']
        if panel['type'] == 'stat' and panel['gridPos']['y'] == 1
    ]

    assert dashboard['time']['from'] == 'now-3d'
    assert len(stat_panels) == 6
    assert {
        'Grid Imported',
        'Grid Exported',
        'Net Grid',
        'Import Cost',
        'Export Revenue',
        'Net Cost',
    }.issubset(titles)
    assert {
        'Daily Grid Energy',
        'Daily Cost and Revenue',
        'Unit Rates over Time',
        'Average by Hour of Day',
        'Cumulative Grid Energy',
    }.issubset(titles)


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
