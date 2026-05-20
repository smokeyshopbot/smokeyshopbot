"""
utils/crypto.py — Unique 3-decimal amount generator for USDT payments.

The bot shows payment amounts with 3 decimals (for example 15.253 USDT)
so users get a clean amount while pending payments still remain unique.
"""

from decimal import Decimal, InvalidOperation, ROUND_CEILING
from database import get_all_pending_unique_usdt_payments

USDT_PAYMENT_QUANT = Decimal("0.001")


def _ceil_to_payment_quant(value: float | str | Decimal) -> Decimal:
    """Round a base payment amount up to the visible 3-decimal USDT amount."""
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    if amount <= 0:
        return USDT_PAYMENT_QUANT
    return amount.quantize(USDT_PAYMENT_QUANT, rounding=ROUND_CEILING)


async def generate_unique_usdt_amount(base_amount: float) -> float:
    """
    Return a unique 3-decimal USDT amount for exact auto-verification.

    The first available amount starts at the requested amount rounded UP to
    3 decimals. If another active pending payment already uses that amount,
    the generator moves by 0.001 USDT until it finds a free slot.
    """
    existing = await get_all_pending_unique_usdt_payments()
    existing_amounts = set()
    for pending in existing:
        try:
            existing_amounts.add(Decimal(str(pending.get("unique_usdt", 0))).quantize(USDT_PAYMENT_QUANT))
        except (InvalidOperation, TypeError, ValueError):
            continue

    base = _ceil_to_payment_quant(base_amount)
    # 2000 slots gives up to +1.999 USDT before falling back, which is plenty
    # for many same-total pending orders while keeping amounts readable.
    for step in range(2000):
        candidate = base + (USDT_PAYMENT_QUANT * step)
        if candidate not in existing_amounts:
            return float(candidate)

    # Extremely unlikely fallback. Still 3-decimal and collision-safe best-effort.
    step = 2000
    while True:
        candidate = base + (USDT_PAYMENT_QUANT * step)
        if candidate not in existing_amounts:
            return float(candidate)
        step += 1
