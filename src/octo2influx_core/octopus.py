from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import logging
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import requests

from octo2influx_core.models import (
    RATE_TYPE_UNITS,
    TariffConfig,
    UsageConfig,
    infer_rate_types,
    parse_optional_datetime,
)


@dataclass(frozen=True)
class ApiPage:
    items: list[dict[str, Any]]
    number: int
    total_count: int | None


class OctopusClient:
    """Small, bounded client for the Octopus REST API."""

    def __init__(self, session: requests.Session, base_url: str,
                 api_key: str, timeout_seconds: int,
                 max_pages: int = 1000):
        self.session = session
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages

    def _url(self, path: str) -> str:
        return f'{self.base_url}/{path.lstrip("/")}'

    @staticmethod
    def _origin(url: str) -> tuple[str, str | None, int | None]:
        parsed = urlsplit(url)
        default_port = 443 if parsed.scheme == 'https' else 80
        return parsed.scheme.lower(), parsed.hostname, parsed.port or default_port

    def get_json(self, url: str,
                 authenticated: bool) -> dict[str, Any]:
        response = self.session.get(
            url,
            auth=(self.api_key, '') if authenticated else None,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f'Octopus API response from {url} is not an object.')
        return payload

    def iter_pages(
            self,
            url: str,
            params: dict[str, Any],
            authenticated: bool,
    ) -> Iterator[ApiPage]:
        """Yield validated pages without retaining the complete response."""
        expected_origin = self._origin(url)
        current_url = url
        current_params: dict[str, Any] | None = params
        seen_urls: set[str] = set()

        for page_number in range(1, self.max_pages + 1):
            response = self.session.get(
                current_url,
                params=current_params,
                auth=(self.api_key, '') if authenticated else None,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(
                    f'Octopus API response from {current_url} is not an object.')

            items = payload.get('results')
            if not isinstance(items, list):
                raise ValueError(
                    f'Octopus API response from {current_url} '
                    'has no results list.')
            total_count = payload.get('count')
            if total_count is not None and not isinstance(total_count, int):
                raise ValueError(
                    f'Octopus API response from {current_url} '
                    'has an invalid count.')

            logging.info(
                f'       ... page {page_number}: {len(items)} rows'
                + (
                    f' of {total_count}'
                    if total_count is not None else ''
                )
            )
            yield ApiPage(items, page_number, total_count)

            next_link = payload.get('next')
            if next_link is None:
                return
            if not isinstance(next_link, str):
                raise ValueError(
                    f'Octopus API response from {current_url} '
                    'has an invalid next link.')

            next_url = urljoin(current_url, next_link)
            if self._origin(next_url) != expected_origin:
                raise ValueError(
                    f'Refusing cross-origin Octopus pagination link: '
                    f'{next_url}')
            if next_url in seen_urls:
                raise ValueError(
                    f'Octopus pagination cycle detected at {next_url}')
            seen_urls.add(next_url)
            current_url = next_url
            current_params = None

        raise ValueError(
            f'Octopus pagination exceeded {self.max_pages} pages for {url}.')

    def consumption_pages(
            self,
            usage: UsageConfig,
            from_iso8601: str,
            to_iso8601: str,
    ) -> Iterator[ApiPage]:
        energy = quote(usage.energy_type, safe='')
        meter_point = quote(usage.meter_point, safe='')
        meter_serial = quote(usage.meter_serial, safe='')
        url = self._url(
            f'{energy}-meter-points/{meter_point}/meters/'
            f'{meter_serial}/consumption/'
        )
        return self.iter_pages(
            url,
            {
                'period_from': from_iso8601,
                'period_to': to_iso8601,
                'page_size': 25000,
                'order_by': 'period',
            },
            authenticated=True,
        )

    def rate_pages(
            self,
            tariff: TariffConfig,
            price_type: str,
            from_iso8601: str,
            to_iso8601: str,
    ) -> Iterator[ApiPage]:
        product_code = quote(tariff.product_code, safe='')
        energy_type = quote(tariff.energy_type, safe='')
        tariff_code = quote(tariff.tariff_code, safe='')
        price_type_path = quote(price_type, safe='')
        url = self._url(
            f'products/{product_code}/{energy_type}-tariffs/'
            f'{tariff_code}/{price_type_path}/'
        )
        return self.iter_pages(
            url,
            {
                'period_from': from_iso8601,
                'period_to': to_iso8601,
                'page_size': 1500,
            },
            authenticated=False,
        )

    def account(self, account_number: str) -> dict[str, Any]:
        account = quote(account_number, safe='')
        return self.get_json(
            self._url(f'accounts/{account}/'),
            authenticated=True,
        )

    def product(self, product_code: str) -> dict[str, Any]:
        product = quote(product_code, safe='')
        return self.get_json(
            self._url(f'products/{product}/'),
            authenticated=False,
        )


def product_code_from_tariff_code(tariff_code: str) -> str:
    parts = tariff_code.split('-')
    if len(parts) < 4 or parts[0] not in {'E', 'G'}:
        raise ValueError(f'Cannot derive product code from {tariff_code}.')
    return '-'.join(parts[2:-1])


def _find_tariff_details(value: Any,
                         tariff_code: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if value.get('code') == tariff_code:
            return value
        for child in value.values():
            found = _find_tariff_details(child, tariff_code)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_tariff_details(child, tariff_code)
            if found is not None:
                return found
    return None


def _rate_types_from_tariff_details(
        details: dict[str, Any] | None,
) -> tuple[str, ...]:
    if not details:
        return ()
    discovered = []
    for link in details.get('links', ()):
        href = link.get('href') if isinstance(link, dict) else None
        if not href:
            continue
        segment = urlsplit(href).path.rstrip('/').split('/')[-1]
        if segment in RATE_TYPE_UNITS and segment not in discovered:
            discovered.append(segment)
    return tuple(discovered)


def _agreement_is_selected(
        agreement: dict[str, Any],
        include_historical: bool,
        now: datetime,
) -> bool:
    valid_from = parse_optional_datetime(agreement.get('valid_from'))
    valid_to = parse_optional_datetime(agreement.get('valid_to'))
    if include_historical:
        return True
    return (
        (valid_from is None or valid_from <= now)
        and (valid_to is None or valid_to > now)
    )


def discover_account_configuration(
        client: OctopusClient,
        account_number: str,
        gas_unit: str,
        include_historical_tariffs: bool = False,
) -> tuple[list[UsageConfig], list[TariffConfig]]:
    """Discover current meter streams and tariff agreements for an account."""
    account = client.account(account_number)
    properties = account.get('properties')
    if not isinstance(properties, list):
        raise ValueError('Octopus account response has no properties list.')

    current_properties = [
        item for item in properties
        if isinstance(item, dict) and not item.get('moved_out_at')
    ]
    if not current_properties:
        raise ValueError('Octopus account has no active property.')

    usage_items: list[UsageConfig] = []
    tariff_items: list[TariffConfig] = []
    product_cache: dict[str, dict[str, Any] | None] = {}
    now = datetime.now(timezone.utc)

    def add_tariffs(meter_point: dict[str, Any], energy_type: str,
                    direction: str) -> None:
        agreements = meter_point.get('agreements') or []
        for agreement in agreements:
            if not isinstance(agreement, dict):
                continue
            tariff_code = agreement.get('tariff_code')
            if not tariff_code or not _agreement_is_selected(
                    agreement, include_historical_tariffs, now):
                continue

            product_code = product_code_from_tariff_code(tariff_code)
            if product_code not in product_cache:
                try:
                    product_cache[product_code] = client.product(product_code)
                except requests.HTTPError as error:
                    logging.warning(
                        f'Could not load Octopus product {product_code}: '
                        f'{error}. Using tariff code metadata.')
                    product_cache[product_code] = None

            product = product_cache[product_code]
            details = _find_tariff_details(product, tariff_code)
            tariff = TariffConfig(
                energy_type=energy_type,
                direction=direction,
                product_code=product_code,
                tariff_code=tariff_code,
                full_name=(
                    product.get('full_name') or product_code
                    if product else product_code
                ),
                display_name=(
                    product.get('display_name') or product_code
                    if product else product_code
                ),
                description=(
                    product.get('description') or ''
                    if product else ''
                ),
                rate_types=_rate_types_from_tariff_details(details),
                agreement_from=parse_optional_datetime(
                    agreement.get('valid_from')),
                agreement_to=parse_optional_datetime(
                    agreement.get('valid_to')),
                source='account',
            )
            tariff_items.append(
                tariff if tariff.rate_types
                else replace(tariff, rate_types=infer_rate_types(tariff))
            )

    for property_item in current_properties:
        electricity_points = (
            property_item.get('electricity_meter_points') or []
        )
        for meter_point in electricity_points:
            if not isinstance(meter_point, dict):
                continue
            direction = 'export' if meter_point.get('is_export') else 'import'
            mpan = meter_point.get('mpan')
            for meter in meter_point.get('meters') or []:
                serial = meter.get('serial_number')
                if mpan and serial:
                    usage_items.append(UsageConfig(
                        energy_type='electricity',
                        direction=direction,
                        meter_point=str(mpan),
                        meter_serial=str(serial),
                        unit='kWh',
                        source='account',
                    ))
            add_tariffs(meter_point, 'electricity', direction)

        gas_points = property_item.get('gas_meter_points') or []
        for meter_point in gas_points:
            if not isinstance(meter_point, dict):
                continue
            mprn = meter_point.get('mprn')
            for meter in meter_point.get('meters') or []:
                serial = meter.get('serial_number')
                if mprn and serial:
                    usage_items.append(UsageConfig(
                        energy_type='gas',
                        direction='import',
                        meter_point=str(mprn),
                        meter_serial=str(serial),
                        unit=gas_unit,
                        source='account',
                    ))
            add_tariffs(meter_point, 'gas', 'import')

    return usage_items, tariff_items
