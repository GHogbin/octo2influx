from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

import dateutil.parser


STANDARD_UNIT_RATE = 'standard-unit-rates'
DAY_UNIT_RATE = 'day-unit-rates'
NIGHT_UNIT_RATE = 'night-unit-rates'
STANDING_CHARGE = 'standing-charges'
EV_OFF_PEAK_UNIT_RATE = 'ev-device-off-peak-unit-rates'
EV_PEAK_UNIT_RATE = 'ev-device-peak-unit-rates'

RATE_TYPE_UNITS = {
    STANDARD_UNIT_RATE: 'p/kWh',
    DAY_UNIT_RATE: 'p/kWh',
    NIGHT_UNIT_RATE: 'p/kWh',
    EV_OFF_PEAK_UNIT_RATE: 'p/kWh',
    EV_PEAK_UNIT_RATE: 'p/kWh',
    STANDING_CHARGE: 'p/day',
}


@dataclass(frozen=True)
class UsageConfig:
    energy_type: str
    direction: str
    meter_point: str
    meter_serial: str
    unit: str
    source: str = 'explicit'

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.energy_type,
            self.direction,
            self.meter_point,
            self.meter_serial,
        )


@dataclass(frozen=True)
class TariffConfig:
    energy_type: str
    direction: str
    product_code: str
    tariff_code: str
    full_name: str
    display_name: str
    description: str
    rate_types: tuple[str, ...] = ()
    payment_method: str | None = None
    materialize_costs: bool = True
    agreement_from: datetime | None = None
    agreement_to: datetime | None = None
    agreement_windows: tuple[
        tuple[datetime | None, datetime | None], ...
    ] = ()
    source: str = 'explicit'

    @property
    def key(self) -> tuple[str, str, str]:
        return self.energy_type, self.direction, self.tariff_code

    @property
    def validity_windows(
            self,
    ) -> tuple[tuple[datetime | None, datetime | None], ...]:
        if self.agreement_windows:
            return self.agreement_windows
        if self.agreement_from is not None or self.agreement_to is not None:
            return ((self.agreement_from, self.agreement_to),)
        return ((None, None),)

    def applies_at(self, timestamp: datetime) -> bool:
        return any(
            (valid_from is None or timestamp >= valid_from)
            and (valid_to is None or timestamp < valid_to)
            for valid_from, valid_to in self.validity_windows
        )

    def coverage_bounds(
            self,
            default_from: datetime,
            default_to: datetime,
    ) -> tuple[datetime, datetime]:
        starts = [
            value for value, _ in self.validity_windows
            if value is not None
        ]
        ends = [
            value for _, value in self.validity_windows
            if value is not None
        ]
        has_open_start = any(
            value is None for value, _ in self.validity_windows)
        has_open_end = any(
            value is None for _, value in self.validity_windows)
        start = (
            default_from if has_open_start or not starts else min(starts)
        )
        end = default_to if has_open_end or not ends else max(ends)
        return max(default_from, start), min(default_to, end)


@dataclass(frozen=True)
class Rate:
    price_type: str
    value_exc_vat: float
    value_inc_vat: float
    valid_from: datetime | None
    valid_to: datetime | None
    payment_method: str | None = None

    @classmethod
    def from_api(cls, price_type: str, row: dict[str, Any]) -> 'Rate':
        valid_from_raw = row.get('valid_from')
        valid_to_raw = row.get('valid_to')
        return cls(
            price_type=price_type,
            value_exc_vat=float(row['value_exc_vat']),
            value_inc_vat=float(row['value_inc_vat']),
            valid_from=(
                dateutil.parser.isoparse(valid_from_raw)
                if valid_from_raw else None
            ),
            valid_to=(
                dateutil.parser.isoparse(valid_to_raw)
                if valid_to_raw else None
            ),
            payment_method=row.get('payment_method'),
        )

    def contains(self, timestamp: datetime) -> bool:
        return (
            (self.valid_from is None or timestamp >= self.valid_from)
            and (self.valid_to is None or timestamp < self.valid_to)
        )


@dataclass
class RateBook:
    rates: dict[str, list[Rate]] = field(default_factory=dict)

    def add_rows(self, price_type: str,
                 rows: list[dict[str, Any]]) -> None:
        parsed = [Rate.from_api(price_type, row) for row in rows]
        self.rates.setdefault(price_type, []).extend(parsed)
        self.rates[price_type].sort(
            key=lambda rate: (
                rate.valid_from
                or datetime.min.replace(tzinfo=timezone.utc)
            ),
        )

    def has_rate_at(self, price_type: str, timestamp: datetime) -> bool:
        return any(
            rate.contains(timestamp)
            for rate in self.rates.get(price_type, ())
        )

    def covers_at(self, price_type: str, timestamp: datetime) -> bool:
        rates = self.rates.get(price_type, ())
        if not rates:
            return False
        starts = [
            rate.valid_from for rate in rates
            if rate.valid_from is not None
        ]
        ends = [
            rate.valid_to for rate in rates
            if rate.valid_to is not None
        ]
        start = None if len(starts) < len(rates) else min(starts)
        end = None if len(ends) < len(rates) else max(ends)
        return (
            (start is None or timestamp >= start)
            and (end is None or timestamp < end)
        )

    def rate_at(self, price_type: str, timestamp: datetime,
                payment_method: str | None = None) -> Rate | None:
        candidates = [
            rate for rate in self.rates.get(price_type, ())
            if rate.contains(timestamp)
        ]
        if payment_method is not None:
            matching = [
                rate for rate in candidates
                if rate.payment_method == payment_method
            ]
            if matching:
                return matching[-1]
            return None

        without_payment_method = [
            rate for rate in candidates if rate.payment_method is None
        ]
        if without_payment_method:
            return without_payment_method[-1]
        return candidates[-1] if len(candidates) == 1 else None


@dataclass(frozen=True)
class RatePeriod:
    price_type: str
    start: time
    end: time

    def contains(self, value: time) -> bool:
        if self.start < self.end:
            return self.start <= value < self.end
        return value >= self.start or value < self.end


@dataclass(frozen=True)
class TariffSchedule:
    timezone_name: str
    default_price_type: str
    periods: tuple[RatePeriod, ...]

    def price_type_at(self, timestamp: datetime) -> str:
        local_time = timestamp.astimezone(
            ZoneInfo(self.timezone_name)
        ).time().replace(tzinfo=None)
        for period in self.periods:
            if period.contains(local_time):
                return period.price_type
        return self.default_price_type


def parse_optional_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = dateutil.parser.isoparse(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def infer_rate_types(tariff: TariffConfig) -> tuple[str, ...]:
    if tariff.rate_types:
        return tariff.rate_types
    if tariff.energy_type == 'electricity' and tariff.tariff_code.startswith(
            'E-2R-'):
        return DAY_UNIT_RATE, NIGHT_UNIT_RATE, STANDING_CHARGE
    return STANDARD_UNIT_RATE, STANDING_CHARGE
