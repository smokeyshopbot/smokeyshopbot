"""
utils/crypto.py — Unique decimal amount generator for USDT payments.

Instead of charging exactly 15 USDT, we charge something like 15.000041
so each payment is uniquely identifiable on-chain without needing the sender's address.
"""

import random
from database import get_all_pending_unique_usdt_payments


async def generate_unique_usdt_amount(base_amount: float) -> float:
    """
    Adds a small random suffix (0.000001–0.000099) to base_amount
    and checks it does not clash with any currently pending payment.
    Retries up to 10 times if there is a collision.
    """
    existing = await get_all_pending_unique_usdt_payments()
    existing_amounts = {round(float(p.get("unique_usdt", 0)), 6) for p in existing}

    for _ in range(10):
        suffix = random.randint(1, 99) * 0.000001
        unique = round(float(base_amount) + suffix, 6)
        if unique not in existing_amounts:
            return unique

    # Fallback: extremely unlikely to reach here.
    return round(float(base_amount) + random.randint(100, 999) * 0.000001, 6)
