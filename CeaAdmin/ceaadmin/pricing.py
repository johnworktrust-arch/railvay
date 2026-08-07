from __future__ import annotations


TELEGRAM_STAR_SAFE_RUB_VALUE = 0.95
TELEGRAM_STARS_BY_PRICE_RUB = {
    299: 319,
    699: 749,
    1490: 1599,
}


def telegram_stars_amount_for_rub(price_rub: int) -> int:
    price = max(0, int(price_rub or 0))
    configured = TELEGRAM_STARS_BY_PRICE_RUB.get(price)
    if configured is not None:
        return configured
    return max(1, int(price / TELEGRAM_STAR_SAFE_RUB_VALUE + 0.9999))
