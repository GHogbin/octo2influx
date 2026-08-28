#!/usr/bin/env python3

import argparse
import json
from pathlib import Path
from textwrap import dedent


DATASOURCE = {'type': 'influxdb', 'uid': '${datasource}'}
PLUGIN_VERSION = '13.0.7'

COLORS = {
    'yellow': '#F2CC0C',
    'red': '#E02F44',
    'blue': '#3274D9',
    'green': '#73BF69',
    'orange': '#FF9830',
    'purple': '#A352CC',
}


def sql(value: str) -> str:
    return dedent(value).strip()


IMPORT_TOTAL = sql('''
    SELECT COALESCE(SUM("kWh"), 0.0) AS "_value"
    FROM "${usage_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "direction" = 'import'
      AND $__timeFilter(time)
''')

EXPORT_TOTAL = sql('''
    SELECT COALESCE(SUM("kWh"), 0.0) AS "_value"
    FROM "${usage_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "direction" = 'export'
      AND $__timeFilter(time)
''')

NET_GRID_TOTAL = sql('''
    SELECT COALESCE(
      SUM(CASE
        WHEN "direction" = 'import' THEN "kWh"
        WHEN "direction" = 'export' THEN -"kWh"
        ELSE 0.0
      END),
      0.0
    ) AS "_value"
    FROM "${usage_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "direction" IN ('import', 'export')
      AND $__timeFilter(time)
''')

IMPORT_COST_TOTAL = sql('''
    SELECT COALESCE(SUM("value_gbp"), 0.0) AS "_value"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "direction" = 'import'
      AND "tariff_code" = '${electricity_import_tariff}'
      AND "cost_type" IN ('usage', 'standing')
      AND $__timeFilter(time)
''')

EXPORT_REVENUE_TOTAL = sql('''
    SELECT COALESCE(
      SUM(CASE
        WHEN "cost_type" = 'usage' THEN "value_gbp"
        ELSE -"value_gbp"
      END),
      0.0
    ) AS "_value"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "direction" = 'export'
      AND "tariff_code" = '${electricity_export_tariff}'
      AND "cost_type" IN ('usage', 'standing')
      AND $__timeFilter(time)
''')

NET_COST_TOTAL = sql('''
    SELECT COALESCE(
      SUM(CASE
        WHEN "direction" = 'import' THEN "value_gbp"
        WHEN "direction" = 'export' AND "cost_type" = 'usage'
          THEN -"value_gbp"
        WHEN "direction" = 'export' AND "cost_type" = 'standing'
          THEN "value_gbp"
        ELSE 0.0
      END),
      0.0
    ) AS "_value"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'electricity'
      AND (
        ("direction" = 'import'
          AND "tariff_code" = '${electricity_import_tariff}')
        OR
        ("direction" = 'export'
          AND "tariff_code" = '${electricity_export_tariff}')
      )
      AND "cost_type" IN ('usage', 'standing')
      AND $__timeFilter(time)
''')

DAILY_IMPORT = sql('''
    SELECT
      date_bin_wallclock(
        INTERVAL '1 day',
        tz(time, '${account_timezone}')
      ) AS time,
      SUM("kWh") AS "Grid imported"
    FROM "${usage_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "direction" = 'import'
      AND $__timeFilter(time)
    GROUP BY 1
    ORDER BY 1
''')

DAILY_EXPORT = sql('''
    SELECT
      date_bin_wallclock(
        INTERVAL '1 day',
        tz(time, '${account_timezone}')
      ) AS time,
      -SUM("kWh") AS "Grid exported"
    FROM "${usage_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "direction" = 'export'
      AND $__timeFilter(time)
    GROUP BY 1
    ORDER BY 1
''')

DAILY_FINANCIALS = sql('''
    SELECT
      date_bin_wallclock(
        INTERVAL '1 day',
        tz(time, '${account_timezone}')
      ) AS time,
      SUM(CASE
        WHEN "direction" = 'import' THEN "value_gbp"
        ELSE 0.0
      END) AS "Import cost",
      SUM(CASE
        WHEN "direction" = 'export' AND "cost_type" = 'usage'
          THEN "value_gbp"
        WHEN "direction" = 'export' AND "cost_type" = 'standing'
          THEN -"value_gbp"
        ELSE 0.0
      END) AS "Export revenue",
      SUM(CASE
        WHEN "direction" = 'import' THEN "value_gbp"
        WHEN "direction" = 'export' AND "cost_type" = 'usage'
          THEN -"value_gbp"
        WHEN "direction" = 'export' AND "cost_type" = 'standing'
          THEN "value_gbp"
        ELSE 0.0
      END) AS "Net cost"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'electricity'
      AND (
        ("direction" = 'import'
          AND "tariff_code" = '${electricity_import_tariff}')
        OR
        ("direction" = 'export'
          AND "tariff_code" = '${electricity_export_tariff}')
      )
      AND $__timeFilter(time)
    GROUP BY 1
    ORDER BY 1
''')

IMPORT_RATES = sql('''
    WITH rates AS (
      SELECT
        date_bin_gapfill(INTERVAL '30 minutes', time) AS time,
        "price_type",
        locf(avg("p/kWh_inc_vat")) / 100.0 AS "Import £/kWh"
      FROM "${tariffs_measurement}"
      WHERE "energy_type" = 'electricity'
        AND "direction" = 'import'
        AND "price_type" IN (
          'standard-unit-rates',
          'day-unit-rates',
          'night-unit-rates',
          'ev-device-off-peak-unit-rates',
          'ev-device-peak-unit-rates'
        )
        AND "tariff_code" = '${electricity_import_tariff}'
        AND time >= $__timeFrom - INTERVAL '2 days'
        AND time <= $__timeTo
      GROUP BY 1, "price_type"
    )
    SELECT * FROM rates
    WHERE time >= date_bin(INTERVAL '30 minutes', $__timeFrom)
    ORDER BY time
''')

EXPORT_RATES = sql('''
    WITH rates AS (
      SELECT
        date_bin_gapfill(INTERVAL '30 minutes', time) AS time,
        "price_type",
        locf(avg("p/kWh_inc_vat")) / 100.0 AS "Export £/kWh"
      FROM "${tariffs_measurement}"
      WHERE "energy_type" = 'electricity'
        AND "direction" = 'export'
        AND "price_type" IN (
          'standard-unit-rates',
          'day-unit-rates',
          'night-unit-rates',
          'ev-device-off-peak-unit-rates',
          'ev-device-peak-unit-rates'
        )
        AND "tariff_code" = '${electricity_export_tariff}'
        AND time >= $__timeFrom - INTERVAL '2 days'
        AND time <= $__timeTo
      GROUP BY 1, "price_type"
    )
    SELECT * FROM rates
    WHERE time >= date_bin(INTERVAL '30 minutes', $__timeFrom)
    ORDER BY time
''')

HOURLY_PROFILE = sql('''
    WITH intervals AS (
      SELECT
        date_bin(INTERVAL '30 minutes', time) AS interval_time,
        date_part('hour', tz(time, '${account_timezone}')) AS hour,
        "direction",
        SUM("kWh") AS kwh
      FROM "${usage_measurement}"
      WHERE "energy_type" = 'electricity'
        AND "direction" IN ('import', 'export')
        AND $__timeFilter(time)
      GROUP BY 1, 2, 3
    )
    SELECT
      hour AS "Hour",
      AVG(CASE WHEN "direction" = 'import' THEN kwh END)
        AS "Average import",
      AVG(CASE WHEN "direction" = 'export' THEN kwh END)
        AS "Average export"
    FROM intervals
    GROUP BY hour
    ORDER BY hour
''')

CUMULATIVE_ENERGY = sql('''
    WITH daily AS (
      SELECT
        date_bin_wallclock(
          INTERVAL '1 day',
          tz(time, '${account_timezone}')
        ) AS day,
        SUM(CASE WHEN "direction" = 'import' THEN "kWh" ELSE 0.0 END)
          AS imported,
        SUM(CASE WHEN "direction" = 'export' THEN "kWh" ELSE 0.0 END)
          AS exported
      FROM "${usage_measurement}"
      WHERE "energy_type" = 'electricity'
        AND "direction" IN ('import', 'export')
        AND $__timeFilter(time)
      GROUP BY 1
    )
    SELECT
      day AS time,
      SUM(imported) OVER (
        ORDER BY day ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ) AS "Cumulative import",
      SUM(exported) OVER (
        ORDER BY day ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ) AS "Cumulative export"
    FROM daily
    ORDER BY day
''')

GAS_DAILY = sql('''
    SELECT
      date_bin_wallclock(
        INTERVAL '1 day',
        tz(time, '${account_timezone}')
      ) AS time,
      SUM("${gas_unit}") AS "Gas ${gas_unit}"
    FROM "${usage_measurement}"
    WHERE "energy_type" = 'gas'
      AND "direction" = 'import'
      AND $__timeFilter(time)
    GROUP BY 1
    ORDER BY 1
''')

GAS_COST_TOTAL = sql('''
    SELECT
      COALESCE(SUM(CASE
        WHEN "cost_type" = 'usage' THEN "value_gbp"
        ELSE 0.0
      END), 0.0) AS "Usage cost",
      COALESCE(SUM(CASE
        WHEN "cost_type" = 'standing' THEN "value_gbp"
        ELSE 0.0
      END), 0.0) AS "Standing charge",
      COALESCE(SUM("value_gbp"), 0.0) AS "Total cost"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'gas'
      AND "direction" = 'import'
      AND "tariff_code" = '${gas_tariff}'
      AND $__timeFilter(time)
''')

LATEST_SYNC = sql('''
    SELECT
      time AS "Last run",
      "status" AS "Status",
      "successful_streams" AS "Successful streams",
      "failed_streams" AS "Failed streams",
      "duration_seconds" AS "Duration (seconds)"
    FROM "${status_measurement}"
    ORDER BY time DESC
    LIMIT 1
''')

METER_HISTORY = sql('''
    SELECT
      date_bin_wallclock(
        INTERVAL '1 day',
        tz(time, '${account_timezone}')
      ) AS time,
      "meter_point",
      SUM("kWh") AS "Imported kWh"
    FROM "${usage_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "direction" = 'import'
      AND $__timeFilter(time)
    GROUP BY 1, "meter_point"
    ORDER BY 1
''')

TARIFF_COMPARISON = sql('''
    SELECT
      "energy_type" AS "Energy",
      "direction" AS "Direction",
      "tariff_code" AS "Tariff",
      SUM(CASE
        WHEN "cost_type" = 'usage' THEN "value_gbp"
        ELSE 0.0
      END) AS "Usage value",
      SUM(CASE
        WHEN "cost_type" = 'standing' THEN "value_gbp"
        ELSE 0.0
      END) AS "Standing charge",
      SUM("value_gbp") AS "Gross value"
    FROM "${cost_measurement}"
    WHERE $__timeFilter(time)
    GROUP BY "energy_type", "direction", "tariff_code"
    ORDER BY "energy_type", "direction", "Gross value"
''')

IMPORT_TARIFF_TIMELINE = sql('''
    SELECT time, "display_name" AS "Import tariff"
    FROM "${tariffs_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "direction" = 'import'
      AND "price_type" = 'standing-charges'
      AND $__timeFilter(time)
    ORDER BY time
''')

EXPORT_TARIFF_TIMELINE = sql('''
    SELECT time, "display_name" AS "Export tariff"
    FROM "${tariffs_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "direction" = 'export'
      AND "price_type" = 'standing-charges'
      AND $__timeFilter(time)
    ORDER BY time
''')

GAS_TARIFF_TIMELINE = sql('''
    SELECT time, "display_name" AS "Gas tariff"
    FROM "${tariffs_measurement}"
    WHERE "energy_type" = 'gas'
      AND "direction" = 'import'
      AND "price_type" = 'standing-charges'
      AND $__timeFilter(time)
    ORDER BY time
''')


def target(query: str, ref_id: str = 'A',
           output_format: str = 'time_series') -> dict:
    return {
        'datasource': DATASOURCE.copy(),
        'editorMode': 'code',
        'format': output_format,
        'rawQuery': True,
        'rawSql': query,
        'refId': ref_id,
    }


def row(panel_id: int, title: str, y: int) -> dict:
    return {
        'collapsed': False,
        'gridPos': {'h': 1, 'w': 24, 'x': 0, 'y': y},
        'id': panel_id,
        'panels': [],
        'title': title,
        'type': 'row',
    }


def stat(panel_id: int, title: str, query: str, x: int, y: int,
         width: int, color: str, unit: str,
         height: int = 4, time_from: str | None = None) -> dict:
    panel = {
        'datasource': DATASOURCE.copy(),
        'fieldConfig': {
            'defaults': {
                'color': {'fixedColor': color, 'mode': 'fixed'},
                'decimals': 2,
                'mappings': [],
                'thresholds': {
                    'mode': 'absolute',
                    'steps': [{'color': color, 'value': None}],
                },
                'unit': unit,
            },
            'overrides': [],
        },
        'gridPos': {'h': height, 'w': width, 'x': x, 'y': y},
        'id': panel_id,
        'options': {
            'colorMode': 'background',
            'graphMode': 'none',
            'justifyMode': 'center',
            'orientation': 'horizontal',
            'reduceOptions': {
                'calcs': ['lastNotNull'],
                'fields': '',
                'values': False,
            },
            'textMode': 'value_and_name',
            'wideLayout': True,
        },
        'pluginVersion': PLUGIN_VERSION,
        'targets': [target(query, output_format='table')],
        'title': title,
        'type': 'stat',
    }
    if time_from:
        panel['timeFrom'] = time_from
    return panel


def timeseries(panel_id: int, title: str, targets: list[dict],
               x: int, y: int, width: int, height: int,
               unit: str, draw_style: str = 'line',
               stacking: str = 'none', fill_opacity: int = 20,
               description: str = '') -> dict:
    panel = {
        'datasource': DATASOURCE.copy(),
        'fieldConfig': {
            'defaults': {
                'color': {'mode': 'palette-classic'},
                'custom': {
                    'axisBorderShow': False,
                    'axisCenteredZero': False,
                    'axisColorMode': 'text',
                    'axisPlacement': 'auto',
                    'barAlignment': 0,
                    'drawStyle': draw_style,
                    'fillOpacity': fill_opacity,
                    'gradientMode': 'opacity',
                    'hideFrom': {
                        'legend': False,
                        'tooltip': False,
                        'viz': False,
                    },
                    'lineInterpolation': 'linear',
                    'lineWidth': 1,
                    'pointSize': 3,
                    'scaleDistribution': {'type': 'linear'},
                    'showPoints': 'never',
                    'spanNulls': True,
                    'stacking': {'group': 'A', 'mode': stacking},
                    'thresholdsStyle': {'mode': 'off'},
                },
                'mappings': [],
                'thresholds': {
                    'mode': 'absolute',
                    'steps': [{'color': 'green', 'value': None}],
                },
                'unit': unit,
            },
            'overrides': [],
        },
        'gridPos': {'h': height, 'w': width, 'x': x, 'y': y},
        'id': panel_id,
        'options': {
            'legend': {
                'calcs': ['min', 'max', 'mean', 'sum'],
                'displayMode': 'table',
                'placement': 'bottom',
                'showLegend': True,
            },
            'tooltip': {'mode': 'multi', 'sort': 'desc'},
        },
        'pluginVersion': PLUGIN_VERSION,
        'targets': targets,
        'title': title,
        'type': 'timeseries',
    }
    if description:
        panel['description'] = description
    return panel


def bar_chart(panel_id: int, title: str, query: str,
              x: int, y: int, width: int, height: int,
              unit: str) -> dict:
    return {
        'datasource': DATASOURCE.copy(),
        'fieldConfig': {
            'defaults': {
                'color': {'mode': 'palette-classic'},
                'mappings': [],
                'unit': unit,
            },
            'overrides': [],
        },
        'gridPos': {'h': height, 'w': width, 'x': x, 'y': y},
        'id': panel_id,
        'options': {
            'barRadius': 0,
            'barWidth': 0.8,
            'fullHighlight': False,
            'groupWidth': 0.7,
            'legend': {
                'calcs': ['mean', 'max'],
                'displayMode': 'table',
                'placement': 'bottom',
                'showLegend': True,
            },
            'orientation': 'auto',
            'showValue': 'never',
            'stacking': 'none',
            'tooltip': {'mode': 'multi', 'sort': 'desc'},
            'xTickLabelRotation': 0,
        },
        'pluginVersion': PLUGIN_VERSION,
        'targets': [target(query, output_format='table')],
        'title': title,
        'type': 'barchart',
    }


def table_panel(panel_id: int, title: str, query: str,
                x: int, y: int, width: int, height: int) -> dict:
    return {
        'datasource': DATASOURCE.copy(),
        'fieldConfig': {
            'defaults': {
                'color': {'mode': 'thresholds'},
                'custom': {
                    'align': 'auto',
                    'cellOptions': {'type': 'auto'},
                    'inspect': False,
                },
                'mappings': [],
                'thresholds': {
                    'mode': 'absolute',
                    'steps': [{'color': 'green', 'value': None}],
                },
            },
            'overrides': [],
        },
        'gridPos': {'h': height, 'w': width, 'x': x, 'y': y},
        'id': panel_id,
        'options': {
            'cellHeight': 'sm',
            'footer': {'enablePagination': False, 'show': False},
            'showHeader': True,
            'sortBy': [],
        },
        'pluginVersion': PLUGIN_VERSION,
        'targets': [target(query, output_format='table')],
        'title': title,
        'type': 'table',
    }


def state_timeline(panel_id: int, title: str, targets: list[dict],
                   x: int, y: int, width: int, height: int) -> dict:
    return {
        'datasource': DATASOURCE.copy(),
        'fieldConfig': {
            'defaults': {
                'color': {'mode': 'palette-classic'},
                'custom': {
                    'fillOpacity': 80,
                    'lineWidth': 0,
                    'spanNulls': True,
                },
                'mappings': [],
            },
            'overrides': [],
        },
        'gridPos': {'h': height, 'w': width, 'x': x, 'y': y},
        'id': panel_id,
        'options': {
            'alignValue': 'left',
            'legend': {
                'displayMode': 'list',
                'placement': 'bottom',
                'showLegend': True,
            },
            'mergeValues': True,
            'rowHeight': 0.8,
            'showValue': 'auto',
            'tooltip': {'mode': 'single', 'sort': 'none'},
        },
        'pluginVersion': PLUGIN_VERSION,
        'targets': targets,
        'title': title,
        'type': 'state-timeline',
    }


def variables(include_history: bool = False) -> list[dict]:
    values = [{
        'current': {
            'selected': True,
            'text': 'InfluxDB 3',
            'value': 'octo2influx-influxdb',
        },
        'hide': 0,
        'includeAll': False,
        'label': 'Data source',
        'multi': False,
        'name': 'datasource',
        'options': [],
        'query': 'influxdb',
        'refresh': 1,
        'regex': '',
        'skipUrlSync': False,
        'type': 'datasource',
    }]
    if include_history:
        values.append({
            'auto': False,
            'current': {
                'selected': True,
                'text': '365d',
                'value': '365d',
            },
            'hide': 0,
            'label': 'History duration',
            'name': 'HistoryDuration',
            'options': [
                {'selected': False, 'text': '30d', 'value': '30d'},
                {'selected': False, 'text': '90d', 'value': '90d'},
                {'selected': True, 'text': '365d', 'value': '365d'},
                {'selected': False, 'text': '2y', 'value': '2y'},
                {'selected': False, 'text': '5y', 'value': '5y'},
            ],
            'query': '30d,90d,365d,2y,5y',
            'refresh': 2,
            'skipUrlSync': False,
            'type': 'interval',
        })

    values.extend([
        textbox_variable('account_timezone', 'Europe/London',
                         'Account timezone'),
        textbox_variable('usage_measurement', 'octopus-usage'),
        textbox_variable('tariffs_measurement', 'octopus-tariffs'),
        textbox_variable('cost_measurement', 'octopus-costs'),
        textbox_variable('status_measurement', 'octopus-sync-status'),
        {
            'current': {'selected': True, 'text': 'm3', 'value': 'm3'},
            'hide': 0,
            'label': 'Gas unit',
            'name': 'gas_unit',
            'options': [
                {'selected': True, 'text': 'm3', 'value': 'm3'},
                {'selected': False, 'text': 'kWh', 'value': 'kWh'},
            ],
            'query': 'm3,kWh',
            'skipUrlSync': False,
            'type': 'custom',
        },
        tariff_variable(
            'electricity_import_tariff', 'electricity', 'import'),
        tariff_variable(
            'electricity_export_tariff', 'electricity', 'export'),
        tariff_variable('gas_tariff', 'gas', 'import'),
    ])
    return values


def textbox_variable(name: str, value: str,
                     label: str | None = None) -> dict:
    variable = {
        'current': {'selected': True, 'text': value, 'value': value},
        'hide': 0,
        'name': name,
        'options': [{'selected': True, 'text': value, 'value': value}],
        'query': value,
        'skipUrlSync': False,
        'type': 'textbox',
    }
    if label:
        variable['label'] = label
    return variable


def tariff_variable(name: str, energy_type: str,
                    direction: str) -> dict:
    query = sql(f'''
        SELECT DISTINCT (
          "display_name" || ' [' || right("tariff_code", 1) || ']'
          || '#@#' || "tariff_code"
        ) AS "display_value"
        FROM "${{tariffs_measurement}}"
        WHERE "energy_type" = '{energy_type}'
          AND "direction" = '{direction}'
    ''')
    return {
        'current': {},
        'datasource': DATASOURCE.copy(),
        'definition': query,
        'hide': 0,
        'includeAll': False,
        'multi': False,
        'name': name,
        'options': [],
        'query': query,
        'rawSql': query,
        'refresh': 2,
        'regex': '/(?<text>.+)#@#(?<value>.+)/g',
        'skipUrlSync': False,
        'sort': 1,
        'type': 'query',
    }


def base_dashboard(title: str, uid: str, panels: list[dict],
                   default_from: str,
                   include_history: bool = False) -> dict:
    return {
        'annotations': {
            'list': [{
                'builtIn': 1,
                'datasource': {'type': 'datasource', 'uid': 'grafana'},
                'enable': True,
                'hide': True,
                'iconColor': 'rgba(0, 211, 255, 1)',
                'name': 'Annotations & Alerts',
                'target': {
                    'limit': 100,
                    'matchAny': False,
                    'tags': [],
                    'type': 'dashboard',
                },
                'type': 'dashboard',
            }],
        },
        'editable': True,
        'fiscalYearStartMonth': 0,
        'graphTooltip': 1,
        'id': None,
        'links': [],
        'panels': panels,
        'refresh': '1h',
        'schemaVersion': 39,
        'tags': ['octopus-energy', 'octo2influx'],
        'templating': {'list': variables(include_history)},
        'time': {'from': default_from, 'to': 'now-18h'},
        'timepicker': {
            'refresh_intervals': [
                '5m', '15m', '30m', '1h', '2h', '1d',
            ],
            'time_options': [
                '24h', '2d', '7d', '30d', '90d', '1y', '5y',
            ],
        },
        'timezone': 'browser',
        'title': title,
        'uid': uid,
        'version': 1,
        'weekStart': 'monday',
    }


def build_overview_dashboard() -> dict:
    panels = [
        row(100, 'At-a-glance', 0),
        stat(1, 'Grid Imported', IMPORT_TOTAL, 0, 1, 4,
             COLORS['red'], 'kwatth'),
        stat(2, 'Grid Exported', EXPORT_TOTAL, 4, 1, 4,
             COLORS['blue'], 'kwatth'),
        stat(3, 'Net Grid', NET_GRID_TOTAL, 8, 1, 4,
             COLORS['orange'], 'kwatth'),
        stat(4, 'Import Cost', IMPORT_COST_TOTAL, 12, 1, 4,
             COLORS['green'], 'currencyGBP'),
        stat(5, 'Export Revenue', EXPORT_REVENUE_TOTAL, 16, 1, 4,
             COLORS['purple'], 'currencyGBP'),
        stat(6, 'Net Cost', NET_COST_TOTAL, 20, 1, 4,
             COLORS['yellow'], 'currencyGBP'),
        row(101, 'Daily Energy', 5),
        timeseries(
            7,
            'Daily Grid Energy',
            [
                target(DAILY_IMPORT, 'Import'),
                target(DAILY_EXPORT, 'Export'),
            ],
            0, 6, 24, 10, 'kwatth',
            draw_style='bars', stacking='normal', fill_opacity=80,
            description=(
                'Import is positive and export is negative. '
                'Daily boundaries follow the account timezone.'
            ),
        ),
        row(102, 'Financials and Unit Rates', 16),
        timeseries(
            8,
            'Daily Cost and Revenue',
            [target(DAILY_FINANCIALS)],
            0, 17, 14, 9, 'currencyGBP',
            draw_style='bars', fill_opacity=55,
        ),
        timeseries(
            9,
            'Unit Rates over Time',
            [
                target(IMPORT_RATES, 'Import'),
                target(EXPORT_RATES, 'Export'),
            ],
            14, 17, 10, 9, 'currencyGBP',
            draw_style='line', fill_opacity=15,
        ),
        row(103, 'Usage Patterns', 26),
        bar_chart(
            10, 'Average by Hour of Day',
            HOURLY_PROFILE, 0, 27, 8, 8, 'kwatth'),
        timeseries(
            11,
            'Cumulative Grid Energy',
            [target(CUMULATIVE_ENERGY)],
            8, 27, 16, 8, 'kwatth',
            draw_style='line', fill_opacity=12,
        ),
        row(104, 'Gas', 35),
        timeseries(
            12,
            'Daily Gas Usage (${gas_unit})',
            [target(GAS_DAILY)],
            0, 36, 16, 8, 'short',
            draw_style='bars', fill_opacity=70,
        ),
        stat(
            13, 'Gas Cost', GAS_COST_TOTAL, 16, 36, 8,
            COLORS['orange'], 'currencyGBP', height=8),
        row(105, 'Ingestion Health', 44),
        stat(
            14, 'Latest Synchronization', LATEST_SYNC, 0, 45, 24,
            COLORS['green'], 'short', height=5),
    ]
    return base_dashboard(
        'octo2influx — Overview',
        'octo2influx-overview',
        panels,
        'now-30d',
    )


def build_historical_dashboard() -> dict:
    panels = [
        row(200, 'Historical Financials', 0),
        timeseries(
            1,
            'Daily Electricity Cost and Revenue',
            [target(DAILY_FINANCIALS)],
            0, 1, 14, 9, 'currencyGBP',
            draw_style='bars', fill_opacity=60,
        ),
        timeseries(
            2,
            'Unit Rates over Time',
            [
                target(IMPORT_RATES, 'Import'),
                target(EXPORT_RATES, 'Export'),
            ],
            14, 1, 10, 9, 'currencyGBP',
            draw_style='line', fill_opacity=15,
        ),
        row(201, 'Metering History', 10),
        timeseries(
            3,
            'Daily Grid Import and Export',
            [
                target(DAILY_IMPORT, 'Import'),
                target(DAILY_EXPORT, 'Export'),
            ],
            0, 11, 24, 9, 'kwatth',
            draw_style='bars', stacking='normal', fill_opacity=75,
        ),
        timeseries(
            4,
            'Import by Meter Point',
            [target(METER_HISTORY)],
            0, 20, 12, 8, 'kwatth',
            draw_style='bars', stacking='normal', fill_opacity=60,
        ),
        bar_chart(
            5, 'Average Import and Export by Hour',
            HOURLY_PROFILE, 12, 20, 12, 8, 'kwatth'),
        row(202, 'Tariff History and Comparison', 28),
        state_timeline(
            6,
            'Tariff Timeline',
            [
                target(
                    IMPORT_TARIFF_TIMELINE,
                    'Import',
                    output_format='table',
                ),
                target(
                    EXPORT_TARIFF_TIMELINE,
                    'Export',
                    output_format='table',
                ),
                target(
                    GAS_TARIFF_TIMELINE,
                    'Gas',
                    output_format='table',
                ),
            ],
            0, 29, 24, 7,
        ),
        table_panel(
            7, 'Tariff Comparison',
            TARIFF_COMPARISON, 0, 36, 12, 9),
        timeseries(
            8,
            'Cumulative Energy Totals',
            [target(CUMULATIVE_ENERGY)],
            12, 36, 12, 9, 'kwatth',
            draw_style='line', fill_opacity=10,
        ),
        row(203, 'Gas History', 45),
        timeseries(
            9,
            'Daily Gas Usage (${gas_unit})',
            [target(GAS_DAILY)],
            0, 46, 16, 8, 'short',
            draw_style='bars', fill_opacity=70,
        ),
        stat(
            10, 'Gas Cost', GAS_COST_TOTAL, 16, 46, 8,
            COLORS['orange'], 'currencyGBP', height=8,
            time_from='$HistoryDuration'),
        row(204, 'Ingestion Health', 54),
        stat(
            11, 'Latest Synchronization', LATEST_SYNC, 0, 55, 24,
            COLORS['green'], 'short', height=5),
    ]
    for panel in panels:
        if panel.get('type') != 'row' and panel['id'] not in {11}:
            panel.setdefault('timeFrom', '$HistoryDuration')
    return base_dashboard(
        'octo2influx — Historical Analysis',
        'octo2influx-history',
        panels,
        'now-5y',
        include_history=True,
    )


OUTPUTS = {
    Path(__file__).with_name('dashboard.json'): build_overview_dashboard,
    Path(__file__).with_name(
        'historical-dashboard.json'
    ): build_historical_dashboard,
}


def serialized_dashboard(builder) -> str:
    return json.dumps(
        builder(),
        ensure_ascii=False,
        indent=2,
    ) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--check',
        action='store_true',
        help='fail if generated dashboard files are stale',
    )
    args = parser.parse_args()

    stale = []
    for output, builder in OUTPUTS.items():
        expected = serialized_dashboard(builder)
        if args.check:
            if not output.exists() or output.read_text(
                    encoding='utf-8') != expected:
                stale.append(output)
        else:
            output.write_text(expected, encoding='utf-8')

    if stale:
        for output in stale:
            print(f'Generated dashboard is stale: {output}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
