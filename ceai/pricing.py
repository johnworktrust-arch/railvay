from __future__ import annotations


TELEGRAM_STARS_RUB_RATE = 2


def telegram_stars_amount_for_rub(price_rub: int) -> int:
    price = max(0, int(price_rub or 0))
    return max(1, (price + TELEGRAM_STARS_RUB_RATE - 1) // TELEGRAM_STARS_RUB_RATE)
