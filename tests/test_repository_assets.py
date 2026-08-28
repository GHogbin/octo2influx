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
    assert 'GF_AUTH_ANONYMOUS_ENABLED=false' in (
        services['grafana']['environment']
    )
    assert services['octo2influx']['volumes'][0].endswith(':ro')
    assert services['octo2influx']['environment']['RETRY_FREQ'] == '5m'


def test_dashboard_is_portable_and_uses_safe_time_queries():
    dashboard_text = (
        ROOT / 'grafana' / 'dashboard.json'
    ).read_text(encoding='utf-8')
    dashboard = json.loads(dashboard_text)
    queries = list(dashboard_queries(dashboard))

    assert dashboard['id'] is None
    assert dashboard['__inputs'][0]['name'] == 'DS_INFLUXDB'
    assert 'c8229740-0e9a-4f46-a107-27c0a34b86fb' not in dashboard_text
    assert dashboard_text.count('${DS_INFLUXDB}') > 0

    cost_queries = [
        query for query in queries if 'date_bin_gapfill' in query
    ]
    assert len(cost_queries) == 4
    for query in cost_queries:
        assert "time >= $__timeFrom - INTERVAL '2 days'" in query
        assert 'COALESCE(SUM(r.standing), 0.0)' in query

    daily_queries = [
        query for query in queries
        if "INTERVAL '1 day'" in query
    ]
    assert len(daily_queries) == 3
    for query in daily_queries:
        assert 'date_bin_wallclock' in query
        assert "tz(time, '${account_timezone}')" in query
