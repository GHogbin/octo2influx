import json
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]


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
    assert services['octo2influx']['volumes'][0].endswith(':ro')
    assert services['octo2influx']['environment']['RETRY_FREQ'] == '5m'
    assert services['octo2influx']['depends_on']['influx']['condition'] == (
        'service_healthy'
    )


def test_dashboard_is_portable_and_uses_safe_time_queries():
    dashboard_text = (
        ROOT / 'grafana' / 'dashboard.json'
    ).read_text(encoding='utf-8')
    dashboard = json.loads(dashboard_text)
    queries = list(dashboard_queries(dashboard))

    assert dashboard['id'] is None
    assert 'c8229740-0e9a-4f46-a107-27c0a34b86fb' not in dashboard_text
    assert '${DS_INFLUXDB}' not in dashboard_text
    assert dashboard_text.count('${datasource}') > 0
    assert 'avg(\\"kWh\\")' not in dashboard_text

    cost_queries = [
        query for query in queries
        if 'FROM "${cost_measurement}"' in query
    ]
    assert len(cost_queries) == 5
    for query in cost_queries:
        assert '"cost_type" IN (\'usage\', \'standing\')' in query
        assert 'date_bin_gapfill' not in query
        assert '/ 48.0' not in query

    daily_queries = [
        query for query in queries
        if "INTERVAL '1 day'" in query
    ]
    assert len(daily_queries) == 3
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
    assert any(panel['title'] == 'Latest Synchronization'
               for panel in dashboard['panels'])

    rate_queries = [
        query for query in queries
        if 'unit-rate_£/kWh' in query
    ]
    assert len(rate_queries) == 2
    assert all('date_bin_gapfill' in query for query in rate_queries)
    assert all('locf(' in query for query in rate_queries)
