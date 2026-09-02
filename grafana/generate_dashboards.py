#!/usr/bin/env python3

import argparse
import json
from pathlib import Path
from textwrap import dedent


DATASOURCE = {'type': 'influxdb', 'uid': '${datasource}'}
PLUGIN_VERSION = '13.0.7'
COST_MODEL = 'dispatch-aware-v1'

COLORS = {
    'yellow': '#F2CC0C',
    'red': '#E02F44',
    'blue': '#3274D9',
    'green': '#73BF69',
    'orange': '#FF9830',
    'purple': '#A352CC',
}

DEFAULT_TARIFFS = {
    'electricity_import_tariff': {
        'text': 'Intelligent Octopus Go 12M Fixed [H]',
        'value': 'E-1R-INTELLI-FIX-12M-26-06-13-H',
    },
    'gas_tariff': {
        'text': 'Flexible Octopus [H]',
        'value': 'G-1R-VAR-22-11-01-H',
    },
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

ELECTRICITY_USAGE_COST_TOTAL = sql('''
    SELECT COALESCE(SUM("value_gbp"), 0.0) AS "_value"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "cost_model" = '${cost_model}'
      AND "direction" = 'import'
      AND "tariff_code" = '${electricity_import_tariff}'
      AND "cost_type" = 'usage'
      AND $__timeFilter(time)
''')

ELECTRICITY_STANDING_COST_TOTAL = sql('''
    SELECT COALESCE(SUM("value_gbp"), 0.0) AS "_value"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "cost_model" = '${cost_model}'
      AND "direction" = 'import'
      AND "tariff_code" = '${electricity_import_tariff}'
      AND "cost_type" = 'standing'
      AND $__timeFilter(time)
''')

ELECTRICITY_TOTAL_COST = sql('''
    SELECT COALESCE(SUM("value_gbp"), 0.0) AS "_value"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "cost_model" = '${cost_model}'
      AND "direction" = 'import'
      AND "tariff_code" = '${electricity_import_tariff}'
      AND "cost_type" IN ('usage', 'standing')
      AND $__timeFilter(time)
''')

GAS_KWH_TOTAL = sql('''
    SELECT COALESCE(SUM("billing_consumption_kwh"), 0.0) AS "_value"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'gas'
      AND "cost_model" = '${cost_model}'
      AND "direction" = 'import'
      AND "tariff_code" = '${gas_tariff}'
      AND "cost_type" = 'usage'
      AND $__timeFilter(time)
''')

GAS_TOTAL_COST = sql('''
    SELECT COALESCE(SUM("value_gbp"), 0.0) AS "_value"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'gas'
      AND "cost_model" = '${cost_model}'
      AND "direction" = 'import'
      AND "tariff_code" = '${gas_tariff}'
      AND "cost_type" IN ('usage', 'standing')
      AND $__timeFilter(time)
''')

IMPORT_COST_TOTAL = sql('''
    SELECT COALESCE(SUM("value_gbp"), 0.0) AS "_value"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "cost_model" = '${cost_model}'
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
      AND "cost_model" = '${cost_model}'
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
      AND "cost_model" = '${cost_model}'
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

ELECTRICITY_IMPORT_INTERVAL = sql('''
    SELECT
      date_bin(INTERVAL '${chart_interval}', time) AS time,
      SUM("kWh") AS "Electricity import"
    FROM "${usage_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "direction" = 'import'
      AND $__timeFilter(time)
    GROUP BY 1
    ORDER BY 1
''')

ELECTRICITY_EXPORT_INTERVAL = sql('''
    SELECT
      date_bin(INTERVAL '${chart_interval}', time) AS time,
      -SUM("kWh") AS "Electricity export"
    FROM "${usage_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "direction" = 'export'
      AND $__timeFilter(time)
    GROUP BY 1
    ORDER BY 1
''')

ELECTRICITY_FINANCIALS_INTERVAL = sql('''
    SELECT
      date_bin(INTERVAL '${chart_interval}', time) AS time,
      SUM(CASE
        WHEN "direction" = 'import' THEN "value_gbp"
        ELSE 0.0
      END) AS "Import usage cost",
      SUM(CASE
        WHEN "direction" = 'export' THEN "value_gbp"
        ELSE 0.0
      END) AS "Export usage revenue",
      SUM(CASE
        WHEN "direction" = 'import' THEN "value_gbp"
        WHEN "direction" = 'export' THEN -"value_gbp"
        ELSE 0.0
      END) AS "Net usage cost"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "cost_model" = '${cost_model}'
      AND (
        ("direction" = 'import'
          AND "tariff_code" = '${electricity_import_tariff}')
        OR
        ("direction" = 'export'
          AND "tariff_code" = '${electricity_export_tariff}')
      )
      AND "cost_type" = 'usage'
      AND $__timeFilter(time)
    GROUP BY 1
    ORDER BY 1
''')

ELECTRICITY_INTERVAL_COST = sql('''
    SELECT
      date_bin(INTERVAL '${chart_interval}', time) AS time,
      SUM("value_gbp") AS "Electricity usage cost"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "cost_model" = '${cost_model}'
      AND "direction" = 'import'
      AND "tariff_code" = '${electricity_import_tariff}'
      AND "cost_type" = 'usage'
      AND $__timeFilter(time)
    GROUP BY 1
    ORDER BY 1
''')

IMPORT_RATES = sql('''
    WITH rates AS (
      SELECT
        date_bin_gapfill(INTERVAL '${chart_interval}', time) AS time,
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
    WHERE time >= date_bin(INTERVAL '${chart_interval}', $__timeFrom)
    ORDER BY time
''')

EXPORT_RATES = sql('''
    WITH rates AS (
      SELECT
        date_bin_gapfill(INTERVAL '${chart_interval}', time) AS time,
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
    WHERE time >= date_bin(INTERVAL '${chart_interval}', $__timeFrom)
    ORDER BY time
''')

ELECTRICITY_COST_RATES = sql('''
    SELECT
      date_bin_gapfill(INTERVAL '${chart_interval}', time) AS time,
      "price_type",
      locf(avg("unit_rate_pence")) / 100.0 AS "Electricity £/kWh"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'electricity'
      AND "cost_model" = '${cost_model}'
      AND "direction" = 'import'
      AND "tariff_code" = '${electricity_import_tariff}'
      AND "cost_type" = 'usage'
      AND time >= $__timeFrom
      AND time <= $__timeTo
    GROUP BY 1, "price_type"
    ORDER BY 1
''')

GAS_COST_RATES = sql('''
    SELECT
      date_bin_gapfill(INTERVAL '${chart_interval}', time) AS time,
      locf(avg("unit_rate_pence")) / 100.0 AS "Gas £/kWh"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'gas'
      AND "cost_model" = '${cost_model}'
      AND "direction" = 'import'
      AND "tariff_code" = '${gas_tariff}'
      AND "cost_type" = 'usage'
      AND time >= $__timeFrom
      AND time <= $__timeTo
    GROUP BY 1
    ORDER BY 1
''')

HOURLY_PROFILE = sql('''
    WITH intervals AS (
      SELECT
        date_bin(INTERVAL '1 hour', time) AS interval_time,
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
    WHERE interval_time >= $__timeFrom
      AND interval_time + INTERVAL '1 hour' <= $__timeTo
    GROUP BY hour
    ORDER BY hour
''')

IMPORT_HOURLY_PROFILE = sql('''
    WITH intervals AS (
      SELECT
        date_bin(INTERVAL '1 hour', time) AS interval_time,
        date_part('hour', tz(time, '${account_timezone}')) AS hour,
        SUM("kWh") AS kwh
      FROM "${usage_measurement}"
      WHERE "energy_type" = 'electricity'
        AND "direction" = 'import'
        AND $__timeFilter(time)
      GROUP BY 1, 2
    )
    SELECT
      hour AS "Hour",
      AVG(kwh) AS "Average import"
    FROM intervals
    WHERE interval_time >= $__timeFrom
      AND interval_time + INTERVAL '1 hour' <= $__timeTo
    GROUP BY hour
    ORDER BY hour
''')

CUMULATIVE_ENERGY_INTERVAL = sql('''
    WITH intervals AS (
      SELECT
        date_bin(INTERVAL '${chart_interval}', time) AS interval_time,
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
      interval_time AS time,
      SUM(imported) OVER (
        ORDER BY interval_time
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ) AS "Cumulative import",
      SUM(exported) OVER (
        ORDER BY interval_time
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ) AS "Cumulative export"
    FROM intervals
    ORDER BY interval_time
''')

CUMULATIVE_IMPORT_INTERVAL = sql('''
    WITH intervals AS (
      SELECT
        date_bin(INTERVAL '${chart_interval}', time) AS interval_time,
        SUM("kWh") AS imported
      FROM "${usage_measurement}"
      WHERE "energy_type" = 'electricity'
        AND "direction" = 'import'
        AND $__timeFilter(time)
      GROUP BY 1
    )
    SELECT
      interval_time AS time,
      SUM(imported) OVER (
        ORDER BY interval_time
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ) AS "Cumulative import"
    FROM intervals
    ORDER BY interval_time
''')

GAS_USAGE_INTERVAL = sql('''
    SELECT
      date_bin(INTERVAL '${chart_interval}', time) AS time,
      SUM("${gas_unit}") AS "Gas ${gas_unit}"
    FROM "${usage_measurement}"
    WHERE "energy_type" = 'gas'
      AND "direction" = 'import'
      AND $__timeFilter(time)
    GROUP BY 1
    ORDER BY 1
''')

GAS_KWH_RATE_INTERVAL = sql('''
    SELECT
      date_bin(INTERVAL '${chart_interval}', time) AS time,
      SUM("billing_consumption_kwh") AS "Gas kWh",
      AVG("unit_rate_pence") / 100.0 AS "Gas £/kWh"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'gas'
      AND "cost_model" = '${cost_model}'
      AND "direction" = 'import'
      AND "tariff_code" = '${gas_tariff}'
      AND "cost_type" = 'usage'
      AND $__timeFilter(time)
    GROUP BY 1
    ORDER BY 1
''')

GAS_COST_INTERVAL = sql('''
    SELECT
      date_bin(INTERVAL '${chart_interval}', time) AS time,
      SUM("value_gbp") AS "Gas usage cost"
    FROM "${cost_measurement}"
    WHERE "energy_type" = 'gas'
      AND "cost_model" = '${cost_model}'
      AND "direction" = 'import'
      AND "tariff_code" = '${gas_tariff}'
      AND "cost_type" = 'usage'
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
      AND "cost_model" = '${cost_model}'
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
    WHERE time >= now() - INTERVAL '7 days'
    ORDER BY time DESC
    LIMIT 1
''')

METER_HISTORY = sql('''
    SELECT
      date_bin(INTERVAL '${chart_interval}', time) AS time,
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
    WHERE "cost_model" = '${cost_model}'
      AND $__timeFilter(time)
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

SELECTED_IMPORT_TARIFF_TIMELINE = sql('''
    WITH seed AS (
      SELECT "display_name" AS "Electricity tariff"
      FROM "${tariffs_measurement}"
      WHERE "energy_type" = 'electricity'
        AND "direction" = 'import'
        AND "tariff_code" = '${electricity_import_tariff}'
        AND "price_type" = 'standing-charges'
        AND time >= $__timeFrom - INTERVAL '2 days'
        AND time < $__timeFrom
      ORDER BY time DESC
      LIMIT 1
    ),
    timeline AS (
      SELECT time, "display_name" AS "Electricity tariff"
      FROM "${tariffs_measurement}"
      WHERE "energy_type" = 'electricity'
        AND "direction" = 'import'
        AND "tariff_code" = '${electricity_import_tariff}'
        AND "price_type" = 'standing-charges'
        AND $__timeFilter(time)
    )
    SELECT $__timeFrom AS time, "Electricity tariff" FROM seed
    UNION ALL
    SELECT time, "Electricity tariff" FROM timeline
    ORDER BY time
''')

SELECTED_GAS_TARIFF_TIMELINE = sql('''
    WITH seed AS (
      SELECT "display_name" AS "Gas tariff"
      FROM "${tariffs_measurement}"
      WHERE "energy_type" = 'gas'
        AND "direction" = 'import'
        AND "tariff_code" = '${gas_tariff}'
        AND "price_type" = 'standing-charges'
        AND time >= $__timeFrom - INTERVAL '2 days'
        AND time < $__timeFrom
      ORDER BY time DESC
      LIMIT 1
    ),
    timeline AS (
      SELECT time, "display_name" AS "Gas tariff"
      FROM "${tariffs_measurement}"
      WHERE "energy_type" = 'gas'
        AND "direction" = 'import'
        AND "tariff_code" = '${gas_tariff}'
        AND "price_type" = 'standing-charges'
        AND $__timeFilter(time)
    )
    SELECT $__timeFrom AS time, "Gas tariff" FROM seed
    UNION ALL
    SELECT time, "Gas tariff" FROM timeline
    ORDER BY time
''')

COMPLETED_DISPATCHES = sql('''
    SELECT
      time AS "Start",
      "end" AS "End",
      "duration_minutes" AS "Minutes",
      "source" AS "Source",
      "location" AS "Location",
      "pricing_eligible" AS "Cheap-rate eligible"
    FROM "${dispatch_measurement}"
    WHERE "dispatch_type" = 'completed'
      AND "account_id" = '${dispatch_account}'
      AND $__timeFilter(time)
    ORDER BY time DESC
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
               description: str = '',
               overrides: list[dict] | None = None) -> dict:
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
            'overrides': overrides or [],
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


def variables(include_history: bool = False,
              include_export: bool = True,
              include_gas_unit: bool = True,
              include_chart_interval: bool = False) -> list[dict]:
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
                'text': '7d',
                'value': '7d',
            },
            'hide': 0,
            'label': 'History duration',
            'name': 'HistoryDuration',
            'options': [
                {'selected': False, 'text': '3d', 'value': '3d'},
                {'selected': True, 'text': '7d', 'value': '7d'},
            ],
            'query': '3d,7d',
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
        textbox_variable('dispatch_measurement', 'octopus-dispatches'),
        textbox_variable('status_measurement', 'octopus-sync-status'),
        cost_model_variable(),
        dispatch_account_variable(),
    ])
    if include_gas_unit:
        values.append({
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
        })
    if include_chart_interval:
        values.append({
            'current': {
                'selected': True,
                'text': '30 minutes',
                'value': '30 minutes',
            },
            'hide': 0,
            'label': 'Chart interval',
            'name': 'chart_interval',
            'options': [
                {
                    'selected': True,
                    'text': '30 minutes',
                    'value': '30 minutes',
                },
                {
                    'selected': False,
                    'text': '1 hour',
                    'value': '1 hour',
                },
            ],
            'query': '30 minutes,1 hour',
            'skipUrlSync': False,
            'type': 'custom',
        })
    values.append(
        tariff_variable(
            'electricity_import_tariff', 'electricity', 'import')
    )
    if include_export:
        values.append(
            tariff_variable(
                'electricity_export_tariff', 'electricity', 'export')
        )
    values.append(tariff_variable('gas_tariff', 'gas', 'import'))
    return values


def textbox_variable(name: str, value: str,
                     label: str | None = None,
                     hide: int = 0) -> dict:
    variable = {
        'current': {'selected': True, 'text': value, 'value': value},
        'hide': hide,
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
          AND time >= now() - INTERVAL '24 hours'
        ORDER BY "display_value"
    ''')
    return {
        'current': {
            'selected': True,
            **DEFAULT_TARIFFS.get(name, {}),
        } if name in DEFAULT_TARIFFS else {},
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


def cost_model_variable() -> dict:
    query = sql('''
        SELECT "cost_model"
        FROM "${status_measurement}"
        WHERE "status" = 'success'
          AND "cost_model" IS NOT NULL
          AND time >= now() - INTERVAL '7 days'
        ORDER BY time DESC
        LIMIT 1
    ''')
    return {
        'current': {
            'selected': True,
            'text': COST_MODEL,
            'value': COST_MODEL,
        },
        'datasource': DATASOURCE.copy(),
        'definition': query,
        'hide': 2,
        'includeAll': False,
        'multi': False,
        'name': 'cost_model',
        'options': [],
        'query': query,
        'rawSql': query,
        'refresh': 2,
        'regex': '',
        'skipUrlSync': False,
        'sort': 0,
        'type': 'query',
    }


def dispatch_account_variable() -> dict:
    query = sql('''
        SELECT "account_id"
        FROM "${dispatch_measurement}"
        WHERE "dispatch_type" IN ('poll', 'completed')
          AND time >= now() - INTERVAL '7 days'
        ORDER BY time DESC
        LIMIT 1
    ''')
    return {
        'current': {},
        'datasource': DATASOURCE.copy(),
        'definition': query,
        'hide': 2,
        'includeAll': False,
        'multi': False,
        'name': 'dispatch_account',
        'options': [],
        'query': query,
        'rawSql': query,
        'refresh': 2,
        'regex': '',
        'skipUrlSync': False,
        'sort': 0,
        'type': 'query',
    }


def base_dashboard(title: str, uid: str, panels: list[dict],
                   default_from: str,
                   include_history: bool = False,
                   include_export: bool = True,
                   include_gas_unit: bool = True,
                   include_chart_interval: bool = False) -> dict:
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
        'description': (
            'Core-safe live view. Raw-data ranges are bounded because '
            'InfluxDB 3 Core does not compact Parquet files.'
        ),
        'editable': True,
        'fiscalYearStartMonth': 0,
        'graphTooltip': 1,
        'id': None,
        'links': [],
        'panels': panels,
        'refresh': '1h',
        'schemaVersion': 39,
        'tags': ['octopus-energy', 'octo2influx'],
        'templating': {
            'list': variables(
                include_history,
                include_export,
                include_gas_unit,
                include_chart_interval,
            ),
        },
        'time': {'from': default_from, 'to': 'now-18h'},
        'timepicker': {
            'refresh_intervals': [
                '5m', '15m', '30m', '1h', '2h', '1d',
            ],
            'time_options': [
                '24h', '2d', '3d', '7d',
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
        stat(1, 'Electricity Imported', IMPORT_TOTAL, 0, 1, 4,
             COLORS['red'], 'kwatth'),
        stat(2, 'Electricity Usage Cost',
             ELECTRICITY_USAGE_COST_TOTAL, 4, 1, 4,
             COLORS['green'], 'currencyGBP'),
        stat(3, 'Electricity Standing Charge',
             ELECTRICITY_STANDING_COST_TOTAL, 8, 1, 4,
             COLORS['yellow'], 'currencyGBP'),
        stat(4, 'Electricity Total Cost',
             ELECTRICITY_TOTAL_COST, 12, 1, 4,
             COLORS['blue'], 'currencyGBP'),
        stat(5, 'Gas Used', GAS_KWH_TOTAL, 16, 1, 4,
             COLORS['orange'], 'kwatth'),
        stat(6, 'Gas Total Cost', GAS_TOTAL_COST, 20, 1, 4,
             COLORS['purple'], 'currencyGBP'),
        row(101, 'Costs and Unit Rates', 5),
        timeseries(
            7,
            'Electricity Usage Cost by Interval',
            [target(ELECTRICITY_INTERVAL_COST)],
            0, 6, 12, 9, 'currencyGBP',
            draw_style='bars', fill_opacity=60,
            description=(
                'Usage cost grouped into the selected 30-minute or one-hour '
                'interval. Standing charge remains in the KPI tile.'
            ),
        ),
        timeseries(
            8,
            'Electricity and Gas Unit Rates',
            [
                target(ELECTRICITY_COST_RATES, 'Electricity'),
                target(GAS_COST_RATES, 'Gas'),
            ],
            12, 6, 12, 9, 'currencyGBP',
            draw_style='line', fill_opacity=12,
            description=(
                'Rates used for the selected Intelligent Octopus Go and '
                'Flexible Octopus Direct Debit cost calculations.'
            ),
        ),
        row(102, 'Metering', 15),
        timeseries(
            9,
            'Electricity Import by Interval',
            [target(ELECTRICITY_IMPORT_INTERVAL, 'Import')],
            0, 16, 12, 9, 'kwatth',
            draw_style='bars', fill_opacity=75,
            description=(
                'Smart-meter electricity import grouped into the selected '
                '30-minute or one-hour interval.'
            ),
        ),
        timeseries(
            10,
            'Gas kWh and Tariff Rate by Interval',
            [target(GAS_KWH_RATE_INTERVAL)],
            12, 16, 12, 9, 'kwatth',
            draw_style='bars', fill_opacity=65,
            description=(
                'Gas kWh is estimated from m³ using the configured 11.1868 '
                'factor. The line is the Direct Debit tariff rate.'
            ),
            overrides=[{
                'matcher': {'id': 'byName', 'options': 'Gas £/kWh'},
                'properties': [
                    {'id': 'unit', 'value': 'currencyGBP'},
                    {'id': 'custom.axisPlacement', 'value': 'right'},
                    {'id': 'custom.drawStyle', 'value': 'line'},
                    {'id': 'custom.fillOpacity', 'value': 0},
                    {'id': 'custom.lineWidth', 'value': 2},
                ],
            }],
        ),
        row(103, 'Selected Tariffs', 25),
        state_timeline(
            11,
            'Electricity and Gas Tariffs',
            [
                target(
                    SELECTED_IMPORT_TARIFF_TIMELINE,
                    'Electricity',
                    output_format='table',
                ),
                target(
                    SELECTED_GAS_TARIFF_TIMELINE,
                    'Gas',
                    output_format='table',
                ),
            ],
            0, 26, 24, 6,
        ),
        row(104, 'Usage Patterns', 32),
        bar_chart(
            12, 'Electricity Import by Hour of Day',
            IMPORT_HOURLY_PROFILE, 0, 33, 12, 8, 'kwatth'),
        timeseries(
            13,
            'Cumulative Electricity Import',
            [target(CUMULATIVE_IMPORT_INTERVAL)],
            12, 33, 12, 8, 'kwatth',
            draw_style='line', fill_opacity=12,
        ),
        row(105, 'Ingestion Health', 41),
        stat(
            14, 'Latest Synchronization', LATEST_SYNC, 0, 42, 24,
            COLORS['green'], 'short', height=5),
    ]
    return base_dashboard(
        'octo2influx — Overview',
        'octo2influx-overview',
        panels,
        'now-3d',
        include_export=False,
        include_gas_unit=False,
        include_chart_interval=True,
    )


def build_historical_dashboard() -> dict:
    panels = [
        row(200, 'Historical Financials', 0),
        timeseries(
            1,
            'Electricity Cost and Revenue by Interval',
            [target(ELECTRICITY_FINANCIALS_INTERVAL)],
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
            'Grid Import and Export by Interval',
            [
                target(ELECTRICITY_IMPORT_INTERVAL, 'Import'),
                target(ELECTRICITY_EXPORT_INTERVAL, 'Export'),
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
            [target(CUMULATIVE_ENERGY_INTERVAL)],
            12, 36, 12, 9, 'kwatth',
            draw_style='line', fill_opacity=10,
        ),
        row(205, 'Completed Smart-Charge Dispatches', 45),
        table_panel(
            12, 'Completed Dispatches',
            COMPLETED_DISPATCHES, 0, 46, 24, 8),
        row(203, 'Gas History', 54),
        timeseries(
            9,
            'Gas Usage by Interval (${gas_unit})',
            [target(GAS_USAGE_INTERVAL)],
            0, 55, 12, 8, 'short',
            draw_style='bars', fill_opacity=70,
        ),
        timeseries(
            10, 'Gas Usage Cost by Interval',
            [target(GAS_COST_INTERVAL)],
            12, 55, 12, 8, 'currencyGBP',
            draw_style='bars', fill_opacity=60,
        ),
        row(204, 'Ingestion Health', 63),
        stat(
            11, 'Latest Synchronization', LATEST_SYNC, 0, 64, 24,
            COLORS['green'], 'short', height=5),
    ]
    for panel in panels:
        if panel.get('type') != 'row' and panel['id'] not in {11}:
            panel.setdefault('timeFrom', '$HistoryDuration')
    return base_dashboard(
        'octo2influx — Historical Analysis',
        'octo2influx-history',
        panels,
        'now-7d',
        include_history=True,
        include_chart_interval=True,
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
