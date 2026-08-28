from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import dateutil.parser
from influxdb_client_3 import Point

from octo2influx_core.models import (
    STANDING_CHARGE,
    RateBook,
    TariffConfig,
    TariffSchedule,
    UsageConfig,
    infer_rate_types,
)


@dataclass(frozen=True)
class CostPlan:
    usage: UsageConfig
    tariff: TariffConfig
    rate_book: RateBook
    unit_price_types: tuple[str, ...]
    schedule: TariffSchedule | None
    gas_m3_to_kwh_factor: float | None

    def price_type_at(self, timestamp: datetime) -> str:
        if self.schedule is not None:
            return self.schedule.price_type_at(timestamp)
        return self.unit_price_types[0]

    def billing_consumption_kwh(self, consumption: float) -> float | None:
        if self.usage.energy_type == 'electricity':
            return consumption
        if self.usage.unit == 'kWh':
            return consumption
        if self.gas_m3_to_kwh_factor is None:
            return None
        return consumption * self.gas_m3_to_kwh_factor


def compatible_tariffs(
        usage: UsageConfig,
        tariffs: list[TariffConfig],
) -> list[TariffConfig]:
    return [
        tariff for tariff in tariffs
        if tariff.energy_type == usage.energy_type
        and tariff.direction == usage.direction
    ]


def build_cost_plan(
        usage: UsageConfig,
        tariff: TariffConfig,
        rate_book: RateBook,
        schedules: dict[str, TariffSchedule],
        gas_m3_to_kwh_factor: float | None,
) -> tuple[CostPlan | None, str | None]:
    unit_price_types = tuple(
        price_type for price_type in infer_rate_types(tariff)
        if price_type != STANDING_CHARGE
    )
    if not unit_price_types:
        return None, f'{tariff.tariff_code} has no unit-rate endpoint.'

    schedule = schedules.get(tariff.tariff_code)
    if len(unit_price_types) > 1 and schedule is None:
        return (
            None,
            f'{tariff.tariff_code} has multiple unit rates but no '
            'tariff schedule.',
        )
    if schedule is not None:
        scheduled_types = {
            schedule.default_price_type,
            *(period.price_type for period in schedule.periods),
        }
        unknown = scheduled_types.difference(unit_price_types)
        if unknown:
            return (
                None,
                f'{tariff.tariff_code} schedule references unavailable '
                f'rate types: {sorted(unknown)}.',
            )

    if (
            usage.energy_type == 'gas'
            and usage.unit == 'm3'
            and gas_m3_to_kwh_factor is None):
        return (
            None,
            'Gas usage is in m3 but gas_m3_to_kwh_factor is not configured.',
        )

    return CostPlan(
        usage=usage,
        tariff=tariff,
        rate_book=rate_book,
        unit_price_types=unit_price_types,
        schedule=schedule,
        gas_m3_to_kwh_factor=gas_m3_to_kwh_factor,
    ), None


def usage_cost_point(
        measurement: str,
        row: dict,
        plan: CostPlan,
) -> Point | None:
    interval_start = dateutil.parser.isoparse(row['interval_start'])
    interval_end = dateutil.parser.isoparse(row['interval_end'])
    if not plan.tariff.applies_at(interval_start):
        return None

    price_type = plan.price_type_at(interval_start)
    rate = plan.rate_book.rate_at(
        price_type,
        interval_start,
        plan.tariff.payment_method,
    )
    if rate is None:
        return None

    consumption = float(row['consumption'])
    billing_kwh = plan.billing_consumption_kwh(consumption)
    if billing_kwh is None:
        return None

    midpoint = interval_start + (interval_end - interval_start) / 2
    return (
        Point(measurement)
        .tag('cost_type', 'usage')
        .tag('energy_type', plan.usage.energy_type)
        .tag('direction', plan.usage.direction)
        .tag('meter_point', plan.usage.meter_point)
        .tag('meter_serial', plan.usage.meter_serial)
        .tag('tariff_code', plan.tariff.tariff_code)
        .tag('price_type', price_type)
        .field('value_gbp', billing_kwh * rate.value_inc_vat / 100.0)
        .field('consumption', consumption)
        .field('consumption_unit', plan.usage.unit)
        .field('billing_consumption_kwh', billing_kwh)
        .field('unit_rate_pence', rate.value_inc_vat)
        .time(midpoint)
    )


def _dates_between(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def standing_charge_points(
        measurement: str,
        usage: UsageConfig,
        tariff: TariffConfig,
        rate_book: RateBook,
        from_dt: datetime,
        to_dt: datetime,
        timezone_name: str,
) -> list[Point]:
    timezone = ZoneInfo(timezone_name)
    start_date = from_dt.astimezone(timezone).date()
    end_date = to_dt.astimezone(timezone).date()
    points = []

    for local_date in _dates_between(start_date, end_date):
        local_midnight = datetime.combine(
            local_date,
            time.min,
            tzinfo=timezone,
        )
        if not tariff.applies_at(local_midnight):
            continue
        rate = rate_book.rate_at(
            STANDING_CHARGE,
            local_midnight,
            tariff.payment_method,
        )
        if rate is None:
            continue
        points.append(
            Point(measurement)
            .tag('cost_type', 'standing')
            .tag('energy_type', usage.energy_type)
            .tag('direction', usage.direction)
            .tag('meter_point', usage.meter_point)
            .tag('tariff_code', tariff.tariff_code)
            .tag('price_type', STANDING_CHARGE)
            .field('value_gbp', rate.value_inc_vat / 100.0)
            .field('unit_rate_pence', rate.value_inc_vat)
            .time(local_midnight)
        )

    return points
