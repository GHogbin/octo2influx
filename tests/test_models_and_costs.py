from datetime import datetime, time, timezone

import octo2influx
from octo2influx_core.costs import (
    build_cost_plan,
    standing_charge_points,
    usage_cost_point,
)
from octo2influx_core.models import (
    DAY_UNIT_RATE,
    NIGHT_UNIT_RATE,
    STANDARD_UNIT_RATE,
    STANDING_CHARGE,
    RateBook,
    RatePeriod,
    TariffConfig,
    TariffSchedule,
    UsageConfig,
    infer_rate_types,
)


def tariff(tariff_code='E-1R-TEST-C', rate_types=()):
    return TariffConfig(
        energy_type='electricity',
        direction='import',
        product_code='TEST',
        tariff_code=tariff_code,
        full_name='Test tariff',
        display_name='Test tariff',
        description='',
        rate_types=rate_types,
    )


def usage(energy_type='electricity', unit='kWh'):
    return UsageConfig(
        energy_type=energy_type,
        direction='import',
        meter_point='meter-point',
        meter_serial='meter-serial',
        unit=unit,
    )


def rate_row(value, valid_from='2024-01-01T00:00:00Z',
             valid_to=None):
    return {
        'value_exc_vat': value,
        'value_inc_vat': value,
        'valid_from': valid_from,
        'valid_to': valid_to,
        'payment_method': None,
    }


def test_infer_rate_types_supports_economy7():
    actual = infer_rate_types(tariff('E-2R-TEST-C'))

    assert actual == (
        DAY_UNIT_RATE,
        NIGHT_UNIT_RATE,
        STANDING_CHARGE,
    )


def test_rate_book_selects_rate_by_validity():
    book = RateBook()
    book.add_rows(STANDARD_UNIT_RATE, [
        rate_row(20, valid_to='2024-01-02T00:00:00Z'),
        rate_row(30, valid_from='2024-01-02T00:00:00Z'),
    ])

    first = book.rate_at(
        STANDARD_UNIT_RATE,
        datetime(2024, 1, 1, 12, tzinfo=timezone.utc),
    )
    second = book.rate_at(
        STANDARD_UNIT_RATE,
        datetime(2024, 1, 2, 12, tzinfo=timezone.utc),
    )

    assert first.value_inc_vat == 20
    assert second.value_inc_vat == 30


def test_rate_book_does_not_fallback_to_wrong_payment_method():
    book = RateBook()
    row = rate_row(20)
    row['payment_method'] = 'NON_DIRECT_DEBIT'
    book.add_rows(STANDARD_UNIT_RATE, [row])

    actual = book.rate_at(
        STANDARD_UNIT_RATE,
        datetime(2024, 1, 1, 12, tzinfo=timezone.utc),
        payment_method='DIRECT_DEBIT',
    )

    assert actual is None


def test_historical_tariff_windows_are_merged():
    first = TariffConfig(
        **{
            **tariff().__dict__,
            'agreement_from': datetime(
                2022, 1, 1, tzinfo=timezone.utc),
            'agreement_to': datetime(
                2023, 1, 1, tzinfo=timezone.utc),
            'source': 'account',
        }
    )
    second = TariffConfig(
        **{
            **tariff().__dict__,
            'agreement_from': datetime(
                2024, 1, 1, tzinfo=timezone.utc),
            'agreement_to': None,
            'source': 'account',
        }
    )

    merged = octo2influx.merge_tariff_configs([], [first, second])

    assert len(merged) == 1
    assert len(merged[0].validity_windows) == 2
    assert merged[0].applies_at(
        datetime(2022, 6, 1, tzinfo=timezone.utc))
    assert not merged[0].applies_at(
        datetime(2023, 6, 1, tzinfo=timezone.utc))
    assert merged[0].applies_at(
        datetime(2024, 6, 1, tzinfo=timezone.utc))


def test_overnight_tariff_schedule_selects_night_rate():
    schedule = TariffSchedule(
        timezone_name='Europe/London',
        default_price_type=DAY_UNIT_RATE,
        periods=(
            RatePeriod(
                price_type=NIGHT_UNIT_RATE,
                start=time(23, 30),
                end=time(5, 30),
            ),
        ),
    )

    assert schedule.price_type_at(
        datetime(2024, 1, 1, 1, tzinfo=timezone.utc)
    ) == NIGHT_UNIT_RATE
    assert schedule.price_type_at(
        datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
    ) == DAY_UNIT_RATE


def test_usage_cost_point_materializes_tariff_comparison():
    book = RateBook()
    book.add_rows(STANDARD_UNIT_RATE, [rate_row(25)])
    plan, reason = build_cost_plan(
        usage(),
        tariff(),
        book,
        schedules={},
        gas_m3_to_kwh_factor=None,
    )

    point = usage_cost_point(
        'octopus-costs',
        {
            'consumption': 2,
            'interval_start': '2024-01-01T00:00:00Z',
            'interval_end': '2024-01-01T00:30:00Z',
        },
        plan,
    )

    assert reason is None
    assert 'value_gbp=0.5' in point.to_line_protocol()
    assert 'billing_consumption_kwh=2' in point.to_line_protocol()


def test_gas_cost_requires_explicit_conversion_factor():
    gas_tariff = TariffConfig(
        energy_type='gas',
        direction='import',
        product_code='GAS',
        tariff_code='G-1R-GAS-C',
        full_name='Gas',
        display_name='Gas',
        description='',
    )
    plan, reason = build_cost_plan(
        usage('gas', 'm3'),
        gas_tariff,
        RateBook(),
        schedules={},
        gas_m3_to_kwh_factor=None,
    )

    assert plan is None
    assert 'gas_m3_to_kwh_factor' in reason


def test_standing_charge_is_once_per_local_day_across_dst():
    book = RateBook()
    book.add_rows(STANDING_CHARGE, [
        rate_row(48, valid_from='2024-03-01T00:00:00Z'),
    ])

    points = standing_charge_points(
        'octopus-costs',
        usage(),
        tariff(),
        book,
        datetime(2024, 3, 30, tzinfo=timezone.utc),
        datetime(2024, 4, 1, 22, 59, tzinfo=timezone.utc),
        'Europe/London',
    )

    lines = [point.to_line_protocol() for point in points]
    assert len(points) == 3
    assert all('value_gbp=0.48' in line for line in lines)
    assert all('meter_serial=' not in line for line in lines)
