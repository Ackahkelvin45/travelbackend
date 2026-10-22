from django import template

register = template.Library()


@register.simple_tag
def price_list_panel(package):
    """Draft vs live price list and the client's acceptance checks (package change form)."""
    from bookings.pricing import acceptance_checks, catalog_snapshot
    from packages.models import PriceListVersion, TravelPackage

    if package.pricing_model != TravelPackage.PricingModel.COMPONENT:
        return None
    live = package.price_lists.filter(status=PriceListVersion.Status.LIVE).first()
    checks = acceptance_checks(package) if package.is_active else []
    return {
        "live": live,
        "draft_differs": live is None or live.snapshot != catalog_snapshot(package),
        "checks": checks,
        "passed": sum(c["ok"] for c in checks),
    }
