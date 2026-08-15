"""
payments/money.py
-----------------
Single home for money arithmetic rules.

Every computed amount (balances, deposits, refunds, FX conversions) must pass
through ``quantize()`` before being stored or compared, and through
``to_subunits()`` before being sent to Paystack. Never call ``int(x * 100)``
directly — it truncates unquantized Decimals.
"""

from decimal import Decimal, ROUND_HALF_UP

TWO_PLACES = Decimal("0.01")

# Balances at or below this are treated as fully paid — never demand an
# unchargeable micro-payment caused by rounding.
FULLY_PAID_TOLERANCE = Decimal("0.01")


def quantize(amount) -> Decimal:
    """Normalize any numeric input to a 2-decimal-place Decimal (ROUND_HALF_UP)."""
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    return amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def to_subunits(amount) -> int:
    """Convert a major-unit amount to gateway subunits (pesewas/kobo/cents)."""
    return int(quantize(amount) * 100)
