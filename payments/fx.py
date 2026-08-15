"""
payments/fx.py
--------------
Live exchange rates for gateway charges (Paystack Ghana charges GHS only, so
USD-priced bookings convert at charge time).

Resolution order for a package's effective charge rate:

  1. fx_mode = "manual"  → the package's charge_exchange_rate, always
  2. live provider fetch (two independent providers, tried in order; cached
     FX_CACHE_SECONDS; each fetch sanity-checked against the last known rate)
  3. last-known-good FxRate row, if younger than FX_MAX_STALENESS_HOURS
  4. the package's charge_exchange_rate as a manual fallback
  5. FxUnavailable → callers refuse the charge cleanly (503), never guess

Keep a daily `manage.py refresh_fx_rates` cron running so the DB fallback is
always warm — then even a full provider outage never blocks checkout.

An optional per-package margin (fx_margin_percent) is applied on top of the
market rate to absorb volatility between charge and settlement. Whatever rate
is used, it is snapshotted on the Payment row — every charge is auditable.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import FxRate

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 4


def _parse_er_api(body: dict, quote: str):
    if body.get("result") != "success":
        return None
    return body.get("rates", {}).get(quote)


def _parse_currency_api(body: dict, quote: str):
    return body.get("usd", {}).get(quote.lower())


# Independent keyless providers, tried in order. Both support GHS.
PROVIDERS = [
    ("open.er-api.com", "https://open.er-api.com/v6/latest/{base}", _parse_er_api),
    (
        "currency-api(jsdelivr)",
        "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{base_lower}.min.json",
        _parse_currency_api,
    ),
]


class FxUnavailable(Exception):
    """No trustworthy rate could be resolved — refuse the charge, never guess."""


@dataclass
class ResolvedRate:
    rate: Decimal            # market rate, margin NOT yet applied
    source: str              # provider name / "cache" / "db-fallback" / "manual"
    fetched_at: object       # datetime | None for manual


def _cache_key(base: str, quote: str) -> str:
    return f"fx:{base}:{quote}"


def _passes_sanity_check(base: str, quote: str, rate: Decimal, source: str) -> bool:
    """
    Two independent guards so a corrupted or hijacked feed can never set
    prices:

    1. Absolute band (FX_HARD_BOUNDS): protects even the first-ever fetch on
       a fresh database — a rate outside the plausibility band is rejected no
       matter what any provider claims.
    2. Relative band (FX_SANITY_MAX_DEVIATION_PERCENT): once history exists,
       a fetch deviating too far from the last stored rate is rejected.
    """
    bounds = getattr(settings, "FX_HARD_BOUNDS", {}).get(f"{base}:{quote}")
    if bounds:
        low, high = Decimal(str(bounds[0])), Decimal(str(bounds[1]))
        if not (low <= rate <= high):
            logger.error(
                "FX HARD-BOUND REJECT: %s reported 1 %s = %s %s, outside the "
                "plausibility band [%s, %s]. Ignoring this provider.",
                source, base, rate, quote, low, high,
            )
            from .alerts import alert_admin
            alert_admin(
                f"fx-hard-bound-{base}-{quote}",
                "FX rate feed rejected (hard bound)",
                f"Provider {source} reported 1 {base} = {rate} {quote}, outside "
                f"the configured plausibility band [{low}, {high}]. The rate was "
                "ignored; the fallback chain is serving instead. Investigate the feed.",
            )
            return False

    last = FxRate.objects.filter(base=base, quote=quote).order_by("-fetched_at").first()
    if last is None:
        return True
    max_deviation = Decimal(str(getattr(settings, "FX_SANITY_MAX_DEVIATION_PERCENT", 20)))
    deviation = abs(rate - last.rate) / last.rate * 100
    if deviation > max_deviation:
        logger.error(
            "FX SANITY REJECT: %s reported 1 %s = %s %s, but last known was %s "
            "(%.1f%% deviation, max %s%%). Ignoring this provider.",
            source, base, rate, quote, last.rate, deviation, max_deviation,
        )
        from .alerts import alert_admin
        alert_admin(
            f"fx-sanity-{base}-{quote}",
            "FX rate feed rejected (deviation)",
            f"Provider {source} reported 1 {base} = {rate} {quote}, deviating "
            f"{deviation:.1f}% from the last known {last.rate} (max {max_deviation}%). "
            "The rate was ignored; the fallback chain is serving instead.",
        )
        return False
    return True


def fetch_live_rate(base: str, quote: str) -> ResolvedRate | None:
    """Try each provider in order; persist an FxRate row on the first sane result."""
    for name, url_template, parse in PROVIDERS:
        url = url_template.format(base=base, base_lower=base.lower())
        try:
            response = requests.get(url, timeout=FETCH_TIMEOUT_SECONDS)
            response.raise_for_status()
            raw = parse(response.json(), quote)
            if raw is None:
                logger.warning("FX provider %s returned no %s->%s rate.", name, base, quote)
                continue
            rate = Decimal(str(raw)).quantize(Decimal("0.000001"))
            if rate <= 0:
                continue
        except (requests.RequestException, ValueError, ArithmeticError) as exc:
            logger.warning("FX fetch from %s failed for %s->%s: %s", name, base, quote, exc)
            continue

        if not _passes_sanity_check(base, quote, rate, name):
            continue

        row = FxRate.objects.create(base=base, quote=quote, rate=rate, source=name)
        return ResolvedRate(rate=rate, source=name, fetched_at=row.fetched_at)

    return None


def get_market_rate(base: str, quote: str) -> ResolvedRate | None:
    """Live rate with process-cache and durable DB fallback (no manual layer)."""
    key = _cache_key(base, quote)
    cached = cache.get(key)
    if cached is not None:
        return ResolvedRate(rate=cached["rate"], source="cache", fetched_at=cached["fetched_at"])

    live = fetch_live_rate(base, quote)
    if live is not None:
        cache.set(
            key,
            {"rate": live.rate, "fetched_at": live.fetched_at},
            getattr(settings, "FX_CACHE_SECONDS", 3600),
        )
        return live

    # Providers down / rejected: last-known-good, bounded by staleness.
    max_age = timedelta(hours=getattr(settings, "FX_MAX_STALENESS_HOURS", 24))
    row = (
        FxRate.objects.filter(base=base, quote=quote, fetched_at__gte=timezone.now() - max_age)
        .order_by("-fetched_at")
        .first()
    )
    if row:
        logger.warning("FX using DB fallback for %s->%s from %s", base, quote, row.fetched_at)
        return ResolvedRate(rate=row.rate, source="db-fallback", fetched_at=row.fetched_at)

    return None


def effective_charge_rate(package, quote: str = "GHS") -> tuple[Decimal, str]:
    """
    The rate actually used to charge a package's bookings: market rate plus
    the package's margin, or the manual rate. Returns (rate, source).

    Raises FxUnavailable when nothing trustworthy is available.
    """
    base = package.currency
    if base == quote:
        return Decimal("1"), "identity"

    manual = package.charge_exchange_rate

    if getattr(package, "fx_mode", "live") == "manual":
        if not manual:
            raise FxUnavailable(f"Manual FX mode but no rate set for package {package.pk}.")
        return manual, "manual"

    resolved = get_market_rate(base, quote)
    if resolved is not None:
        margin = getattr(package, "fx_margin_percent", None) or Decimal("0")
        rate = (resolved.rate * (1 + margin / 100)).quantize(Decimal("0.000001"))
        return rate, resolved.source

    if manual:
        logger.warning("FX falling back to manual rate for package %s.", package.pk)
        return manual, "manual-fallback"

    raise FxUnavailable(f"No live, recent or manual FX rate for {base}->{quote}.")
