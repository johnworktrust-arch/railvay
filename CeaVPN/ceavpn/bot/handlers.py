from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import datetime, timezone
from html import escape
from typing import Any, Dict
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    LinkPreviewOptions,
    Message,
    PreCheckoutQuery,
)


class VpnPromoState(StatesGroup):
    waiting_for_code = State()

from ceavpn.config import Settings
from ceavpn.services.app import AppServices
from ceavpn.services.exceptions import BusinessRuleError
from ceavpn.services.referrals import format_rubles_from_kopecks
from ceavpn.vpn_subscription_delivery import (
    delivery_base_url,
    is_subscription_active,
    with_delivery_subscription,
)


# (name, display_price_rub, stars_price) — stars_price kept for future use
TARIFFS = {
    "1": ("1 месяц", 179, 139),
    "3": ("3 месяца", 469, 389),
    "6": ("6 месяцев", 780, 639),
    "12": ("1 год", 1280, 989),
    "120": ("10 лет", 9990, 7790),
}

VPN_PLAN_CODES = {
    "1": "vpn-1m",
    "3": "vpn-3m",
    "6": "vpn-6m",
    "12": "vpn-12m",
    "120": "vpn-10y",
}

LEGACY_PAYMENT_METHODS = frozenset({"sbp", "card", "crypto", "other"})

VPN_MAIN_SCREEN_TEXT = (
    "◉ <b>CEA VPN</b>  ·  центр подключения\n\n"
    "Один аккаунт — интернет на всех ваших устройствах."
)


def main_screen_text(*, trial_available: bool, active_subscription: bool) -> str:
    if active_subscription:
        status = (
            "🟢 <b>Подписка активна</b>\n"
            "Откройте управление VPN, чтобы подключить новое устройство "
            "или скопировать личную ссылку."
        )
    elif trial_available:
        status = (
            "🎁 <b>Начните бесплатно</b>\n"
            "Активируйте 3 пробных дня — оплата и банковская карта не нужны."
        )
    else:
        status = (
            "⚪️ <b>VPN сейчас не подключён</b>\n"
            "Выберите срок подписки — доступ откроется сразу после оплаты."
        )
    return f"{VPN_MAIN_SCREEN_TEXT}\n\n{status}"


def _user_kwargs(event: Message | CallbackQuery) -> Dict[str, Any]:
    user = event.from_user
    return {
        "telegram_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "language_code": user.language_code,
    }


async def _screen(message: Message, text: str, keyboard: InlineKeyboardMarkup) -> None:
    link_preview_options = LinkPreviewOptions(is_disabled=True)
    try:
        await message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
            link_preview_options=link_preview_options,
        )
    except Exception as _exc:
        # MessageNotModified is harmless; other errors fall back to a new message.
        _msg = str(_exc).lower()
        if "message is not modified" not in _msg:
            logging.debug("edit_text failed, falling back to answer: %s", _exc)
        try:
            await message.answer(
                text,
                reply_markup=keyboard,
                parse_mode="HTML",
                link_preview_options=link_preview_options,
            )
        except Exception:
            logging.exception("_screen answer fallback also failed")


def _back(callback_data: str = "vpn:main") -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)]


def about_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    document_buttons: list[InlineKeyboardButton] = []
    if settings.vpn_user_agreement_url.strip():
        document_buttons.append(
            InlineKeyboardButton(
                text="📄 Пользовательское соглашение",
                url=settings.vpn_user_agreement_url,
            )
        )
    if settings.vpn_privacy_policy_url.strip():
        document_buttons.append(
            InlineKeyboardButton(
                text="🔒 Политика конфиденциальности",
                url=settings.vpn_privacy_policy_url,
            )
        )
    if document_buttons:
        rows.append(document_buttons)

    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="🎟 Ввести промокод",
                    callback_data="vpn:promo",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🆘 Написать в поддержку",
                    url=f"https://t.me/{settings.vpn_support_username}",
                )
            ],
            _back(),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_copy_button(subscription_url: str) -> InlineKeyboardButton:
    """Copy a subscription URL without opening Marzban's HTML page."""
    return InlineKeyboardButton(
        text="📋 Скопировать ссылку подписки",
        copy_text=CopyTextButton(text=subscription_url),
    )


def _subscription_landing_url(
    subscription_url: str,
    subscription_base_url: str,
    *,
    client: str,
) -> str:
    try:
        parsed = urlsplit(subscription_url)
        allowed = urlsplit(subscription_base_url)
    except ValueError:
        return ""
    match = re.fullmatch(r"/sub/([A-Za-z0-9._~-]{1,160})/?", parsed.path)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.scheme != allowed.scheme
        or parsed.netloc != allowed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or allowed.scheme != "https"
        or not allowed.netloc
        or allowed.username is not None
        or allowed.password is not None
        or allowed.path not in {"", "/"}
        or allowed.query
        or allowed.fragment
        or match is None
    ):
        return ""
    return urlunsplit(
        ("https", parsed.netloc, f"/{client}/{match.group(1)}", "", "")
    )


def happ_landing_url(subscription_url: str, subscription_base_url: str) -> str:
    """Build the HTTPS bridge that opens an HTTPS subscription in Happ."""
    return _subscription_landing_url(
        subscription_url,
        subscription_base_url,
        client="happ",
    )


def v2box_landing_url(subscription_url: str, subscription_base_url: str) -> str:
    """Build the HTTPS bridge that opens an HTTPS subscription in V2Box."""
    return _subscription_landing_url(
        subscription_url,
        subscription_base_url,
        client="v2box",
    )


def connect_landing_url(subscription_url: str, subscription_base_url: str) -> str:
    """Build the personal HTTPS setup guide URL."""
    return _subscription_landing_url(
        subscription_url,
        subscription_base_url,
        client="connect",
    )


def subscription_connect_button(
    subscription_url: str, subscription_base_url: str
) -> InlineKeyboardButton:
    landing_url = connect_landing_url(subscription_url, subscription_base_url)
    if not landing_url:
        raise ValueError("invalid VPN subscription URL")
    return InlineKeyboardButton(
        text="Подключить VPN 🚀",
        url=landing_url,
    )


def subscription_open_button(
    subscription_url: str, subscription_base_url: str
) -> InlineKeyboardButton:
    landing_url = happ_landing_url(subscription_url, subscription_base_url)
    if not landing_url:
        raise ValueError("invalid VPN subscription URL")
    return InlineKeyboardButton(
        text="🚀 Открыть в Happ",
        url=landing_url,
        style="primary",
    )


def subscription_v2box_button(
    subscription_url: str, subscription_base_url: str
) -> InlineKeyboardButton:
    landing_url = v2box_landing_url(subscription_url, subscription_base_url)
    if not landing_url:
        raise ValueError("invalid VPN subscription URL")
    return InlineKeyboardButton(
        text="✅ Подключить через V2Box",
        url=landing_url,
        style="success",
    )


def happ_subscription_instructions() -> str:
    return (
        "<b>Как подключить:</b>\n"
        "1. Нажмите «Открыть в Happ».\n"
        "2. Подтвердите добавление подписки.\n"
        "3. Выберите сервер CEA VPN и включите соединение.\n\n"
        "Если Happ не открылся, скопируйте ссылку запасной кнопкой и "
        "добавьте её через <b>+</b> → <b>Добавить подписку</b>.\n\n"
        "Если Happ показывает пинг, но интернет не открывается, "
        "нажмите <b>«Открыть в V2Box»</b> — это бесплатный запасной клиент.\n\n"
        "Если в Happ уже есть отдельный сервер «Marz», удалите его — "
        "это старый импорт без обновлений. Правильная подписка обновляется "
        "автоматически."
    )


def main_keyboard(
    *,
    support_username: str,
    trial_available: bool = True,
    active_subscription: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if active_subscription:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="🟢 Управление VPN",
                        callback_data="vpn:subscription",
                        style="success",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔄 Продлить подписку",
                        callback_data="vpn:plans",
                    )
                ],
            ]
        )
    elif trial_available:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🎁 Попробовать 3 дня бесплатно",
                    callback_data="vpn:trial",
                    style="success",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Тарифы и цены",
                    callback_data="vpn:plans",
                    style="primary",
                )
            ]
        )
    else:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="🚀 Подключить VPN",
                        callback_data="vpn:plans",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="👤 Моя подписка",
                        callback_data="vpn:subscription",
                    )
                ],
            ]
        )
    rows.extend([
        [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="vpn:earn")],
        [
            InlineKeyboardButton(text="💬 Поддержка", url=f"https://t.me/{support_username}"),
            InlineKeyboardButton(text="О сервисе", callback_data="vpn:about"),
        ],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


EXTRA_DEVICE_PRICES = {1: 50, 2: 60, 3: 70, 4: 80, 5: 90}
EXTRA_DEVICE_STARS = {1: 30, 2: 40, 3: 50, 4: 60, 5: 70}
MAX_SUBSCRIPTION_DEVICES = 7


def _discounted_prices(
    price_rub: int, price_stars: int, discount: Dict[str, Any] | None
) -> tuple[int, int]:
    if not discount:
        return price_rub, price_stars
    if discount["reward_type"] == "discount_percent":
        multiplier = max(1, 100 - int(discount["reward_value"])) / 100
        return max(1, round(price_rub * multiplier)), max(1, round(price_stars * multiplier))
    if discount["reward_type"] == "discount_fixed":
        discounted_rub = max(1, price_rub - int(discount["reward_value"]))
        return discounted_rub, max(1, round(price_stars * discounted_rub / price_rub))
    return price_rub, price_stars


def plans_keyboard(
    has_active_paid_subscription: bool = False,
    discount: Dict[str, Any] | None = None,
) -> InlineKeyboardMarkup:
    featured_code = "120"

    def tariff_row(code: str) -> list[InlineKeyboardButton]:
        name, price_rub, price_stars = TARIFFS[code]
        shown_rub, shown_stars = _discounted_prices(
            price_rub, price_stars, discount
        )
        prefix = "🔥 " if code == featured_code else ""
        return [
            InlineKeyboardButton(
                text=f"{prefix}{name} — {shown_rub}₽ / {shown_stars} ⭐️",
                callback_data=f"vpn:tariff:{code}",
            )
        ]

    rows = [tariff_row(code) for code in TARIFFS if code != featured_code]
    if has_active_paid_subscription:
        rows.append([
            InlineKeyboardButton(
                text="📱 Докупить устройства",
                callback_data="vpn:add_devices",
            )
        ])
    rows.append(tariff_row(featured_code))
    rows.append(_back())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def add_devices_keyboard(available: int = 5) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{cnt} устр. — {EXTRA_DEVICE_PRICES[cnt]}₽ / {EXTRA_DEVICE_STARS[cnt]} ⭐️",
                callback_data=f"vpn:extra_dev:{cnt}",
            )
        ]
        for cnt in range(1, min(5, max(0, available)) + 1)
    ]
    rows.append(_back("vpn:plans"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def extra_devices_payment_keyboard(count: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="💳 Оплатить картой / СБП",
                callback_data=f"vpn:pay_extra_dev:{count}:platega",
            )
        ],
        [
            InlineKeyboardButton(
                text="⭐ Оплатить звездами",
                callback_data=f"vpn:pay_extra_dev:{count}:stars",
            )
        ],
    ]
    rows.append(_back("vpn:add_devices"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_keyboard(code: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="💳 Оплатить картой / СБП",
                callback_data=f"vpn:payment:{code}:platega",
            )
        ],
        [
            InlineKeyboardButton(
                text="⭐ Оплатить звездами",
                callback_data=f"vpn:payment:{code}:stars",
            )
        ],
    ]
    rows.append(_back("vpn:plans"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def referral_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Вывести", callback_data="vpn:withdraw")],
        _back(),
    ])


def trial_keyboard(channel_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=channel_url)],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="vpn:trial_check")],
        _back(),
    ])


def _channel_username(channel_url: str) -> str:
    """Return @username from a t.me URL, a bare username, or an @-prefixed name."""
    value = channel_url.strip().rstrip("/")
    if value.startswith("@"):
        return "@" + value.lstrip("@")  # normalise: avoid double-@
    if "t.me/" in value:
        return "@" + value.rsplit("/", 1)[-1].lstrip("@")
    return value


def _payment_callback_id(data: str | None, prefix: str) -> int | None:
    value = data or ""
    expected = f"{prefix}:"
    raw_id = value.removeprefix(expected)
    if (
        not value.startswith(expected)
        or not raw_id.isdigit()
        or len(raw_id) > 19
    ):
        return None
    payment_id = int(raw_id)
    if payment_id <= 0 or payment_id > 9_223_372_036_854_775_807:
        return None
    return payment_id


def _admin_demo_authorized(event: CallbackQuery, services: AppServices) -> bool:
    return False


def _format_ends_at(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(ZoneInfo("Europe/Moscow"))
    months = (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    return (
        f"{local.day} {months[local.month - 1]} {local.year} года, "
        f"{local:%H:%M}"
    )


def _plural_ru(value: int, one: str, few: str, many: str) -> str:
    if value % 10 == 1 and value % 100 != 11:
        return one
    if value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14}:
        return few
    return many



def trial_expiry_reminder_screen(
    ends_at: Any,
    *,
    now: datetime | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    parsed_end = (
        ends_at
        if isinstance(ends_at, datetime)
        else datetime.fromisoformat(str(ends_at))
    )
    if parsed_end.tzinfo is None:
        parsed_end = parsed_end.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    total_minutes = max(
        1,
        math.ceil((parsed_end - current).total_seconds() / 60),
    )
    hours, minutes = divmod(total_minutes, 60)
    remaining_parts: list[str] = []
    if hours:
        remaining_parts.append(
            f"{hours} {_plural_ru(hours, 'час', 'часа', 'часов')}"
        )
    if minutes:
        remaining_parts.append(
            f"{minutes} {_plural_ru(minutes, 'минута', 'минуты', 'минут')}"
        )
    remaining = " ".join(remaining_parts) or "меньше минуты"
    text = (
        "<b>Пробный период скоро закончится</b>\n"
        "⚠️\n\n"
        "Статус подписки:\n"
        "<blockquote>"
        f"⌛ <b>Осталось времени:</b> {escape(remaining)}\n"
        f"📅 <b>Дата окончания:</b> {escape(_format_ends_at(parsed_end))} (МСК)"
        "</blockquote>\n"
        "Тариф:\n"
        "<blockquote>"
        "🎁 <b>3 дня бесплатно</b>\n"
        "Трафик: безлимит\n"
        "Устройств: 2"
        "</blockquote>\n"
        "📶 Успейте продлить подписку заранее, чтобы продолжить "
        "пользоваться интернетом без перерыва."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Продлить подписку",
                    callback_data="vpn:plans",
                    style="success",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👤 Моя подписка",
                    callback_data="vpn:subscription",
                )
            ],
        ]
    )
    return text, keyboard


def expired_subscription_reengagement_screen() -> tuple[str, InlineKeyboardMarkup]:
    return (
        "<b>Ваша подписка истекла ⏳</b>\n\n"
        "Продлите подписку, чтобы всегда оставаться на связи 👇",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="vpn:plans")
        ]]),
    )


def personal_discount_reengagement_screen(
    promocode: str,
    *,
    subscription_expired: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    intro = (
        "Ваша подписка истекла, но пока вы не продлили подписку. Мы сохранили "
        "для вас специальное предложение:"
        if subscription_expired
        else "Вы заходили в бот, но пока ничего не подключили. Мы сохранили "
        "для вас специальное предложение:"
    )
    return (
        "🎁 <b>Персональная скидка от CEA VPN</b>\n\n"
        f"{intro}\n\n"
        "<blockquote>"
        f"Промокод: <code>{escape(promocode)}</code>\n"
        "Скидка: 30% на любой тариф"
        "</blockquote>\n\n"
        "Нажмите кнопку ниже — промокод применится автоматически, а в тарифах "
        "сразу появятся цены со скидкой.",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🔥 Забрать скидку 30%",
                callback_data=f"vpn:promo:activate:{promocode}",
                style="success",
            )
        ]]),
    )


def trial_expired_screen(
    ends_at: Any,
) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "<b>Пробный период закончился</b>\n"
        "⚠️\n\n"
        "Статус подписки:\n"
        "<blockquote>"
        "🔴 <b>Пробный период завершён</b>\n"
        f"📅 <b>Дата окончания:</b> {escape(_format_ends_at(ends_at))} (МСК)"
        "</blockquote>\n"
        "Тариф:\n"
        "<blockquote>"
        "🎁 <b>3 дня бесплатно</b>\n"
        "Трафик: безлимит\n"
        "Устройств: 2"
        "</blockquote>\n"
        "📶 Продлите подписку, чтобы снова пользоваться VPN."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Продлить подписку",
                    callback_data="vpn:plans",
                    style="success",
                )
            ]
        ]
    )
    return text, keyboard


def subscription_screen(
    subscription: Dict[str, Any] | None,
    *,
    support_username: str,
    subscription_base_url: str,
    user: Dict[str, Any] | None = None,
    balance_kopecks: int = 0,
    trial_available: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    profile = user or {}
    display_name = (
        str(profile.get("first_name") or "").strip()
        or (
            f"@{str(profile.get('username')).lstrip('@')}"
            if profile.get("username")
            else "Пользователь"
        )
    )
    telegram_id = profile.get("telegram_id")
    telegram_id_text = (
        str(telegram_id) if telegram_id is not None else "не указан"
    )

    user_info_block = (
        "<blockquote>"
        f"📝 Имя: {escape(display_name)}\n"
        f"🆔 ID: {escape(telegram_id_text)}"
        "</blockquote>"
    )

    if subscription is None or subscription.get("status") in {"expired", "disabled"}:
        sub_info_block = "<blockquote>❌ Статус: <b>Нет активной подписки</b></blockquote>"
        footer_text = "💡 Нажмите «Подключить VPN» — выберите тариф для подключения."
        return (
            f"👤 <b>Моя подписка:</b>\n\n"
            f"{user_info_block}\n\n"
            f"{sub_info_block}\n\n"
            f"{footer_text}",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Подключить VPN 🚀",
                            callback_data="vpn:plans",
                            style="primary",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🆘 Поддержка",
                            url=f"https://t.me/{support_username}",
                        )
                    ],
                    _back(),
                ]
            ),
        )

    status = str(subscription.get("status") or "")
    if status == "error":
        sub_info_block = "<blockquote>❌ Статус: <b>Ошибка подключения</b></blockquote>"
        footer_text = "При создании VPN произошла ошибка. Напишите в поддержку — мы разберёмся и восстановим доступ."
        return (
            f"👤 <b>Моя подписка:</b>\n\n"
            f"{user_info_block}\n\n"
            f"{sub_info_block}\n\n"
            f"{footer_text}",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🆘 Поддержка",
                            url=f"https://t.me/{support_username}",
                        )
                    ],
                    _back(),
                ]
            ),
        )

    kind = str(subscription.get("kind") or "")
    billing_kind = str(subscription.get("billing_kind") or "")
    raw_plan_name = str(subscription.get("plan_name") or "")
    is_paid = (
        billing_kind == "paid"
        or subscription.get("plan_id") is not None
        or kind not in {"", "trial"}
    )
    if not is_paid and (
        kind == "trial"
        or raw_plan_name in {"3 бесплатных дня", "Пробная подписка"}
    ):
        plan_name = "Пробная подписка"
    else:
        plan_name = raw_plan_name or "30 дней"

    extra_devices = int(subscription.get("extra_devices") or 0)
    total_devices = int(subscription.get("plan_max_devices") or 2)
    base_devices = max(1, total_devices - extra_devices)
    if extra_devices > 0:
        devices_text = f"{base_devices} + {extra_devices}"
    else:
        devices_text = str(total_devices)

    ends_at = _format_ends_at(subscription["ends_at"])
    subscription_url = str(subscription.get("subscription_url") or "")

    sub_info_block = (
        "<blockquote>"
        f"💎 Тариф: {escape(plan_name)}\n"
        f"📱 Лимит устройств: {devices_text}\n"
        f"📅 Срок действия: {escape(ends_at)} (МСК)"
        "</blockquote>"
    )
    subscription_link_block = (
        f"🔗 <b>VPN-ссылка:</b>\n<code>{escape(subscription_url)}</code>"
        if subscription_url
        else ""
    )
    footer_text = "💡 Нажмите «Подключить VPN» — откроется персональная инструкция для вашего устройства."

    rows: list[list[InlineKeyboardButton]] = []
    if connect_landing_url(subscription_url, subscription_base_url):
        rows.append(
            [subscription_connect_button(subscription_url, subscription_base_url)]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="Подключить VPN 🚀", callback_data="vpn:subscription", style="primary")]
        )
    rows.append(
        [InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="vpn:plans")]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="📱 Подключённые устройства",
                callback_data="vpn:devices:0",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{support_username}")]
    )
    rows.append(_back())

    return (
        f"👤 <b>Моя подписка:</b>\n\n"
        f"{user_info_block}\n\n"
        f"{sub_info_block}\n\n"
        f"{subscription_link_block}\n\n"
        f"{footer_text}",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _device_datetime(value: Any) -> str:
    if not value:
        return "Не определено"
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "Не определено"


def _device_label(device: Dict[str, Any], index: int) -> str:
    model = str(device.get("model") or "Не определено").strip()
    return f"{index}. {model[:42]}"


def connected_devices_screen(
    subscription: Dict[str, Any] | None,
    devices: list[Dict[str, Any]],
    *,
    total: int,
    page: int,
    page_size: int = 5,
) -> tuple[str, InlineKeyboardMarkup]:
    if subscription is None:
        return (
            "📱 <b>Подключённые устройства</b>\n\n"
            "Нет активной подписки для управления устройствами.",
            InlineKeyboardMarkup(inline_keyboard=[_back("vpn:subscription")]),
        )
    limit = max(1, int(subscription.get("plan_max_devices") or 2))
    lines = [
        "📱 <b>Подключённые устройства</b>",
        "",
        f"Ваша подписка доступна на <b>{limit} устройствах</b>.",
        f"Подключено: <b>{total} из {limit}</b>.",
        f"Можно докупить до {MAX_SUBSCRIPTION_DEVICES} устройств.",
        "",
    ]
    if not devices:
        lines.append("Пока ни одно устройство не подключило подписку.")
    for offset, device in enumerate(devices, start=page * page_size + 1):
        lines.extend(
            [
                f"<b>{offset}.</b>",
                f"└ 📱 Модель: {escape(str(device.get('model') or 'Не определено'))}",
                f"└ 🧠 Платформа: {escape(str(device.get('platform') or 'Не определено'))}",
                f"└ 🔄 Обновлено: {_device_datetime(device.get('last_seen_at'))}",
                "",
            ]
        )
    rows: list[list[InlineKeyboardButton]] = []
    if limit < MAX_SUBSCRIPTION_DEVICES:
        rows.append([InlineKeyboardButton(text="➕ Докупить устройство", callback_data="vpn:add_devices")])
    if total:
        rows.append([InlineKeyboardButton(text="🗑 Отвязать устройство", callback_data=f"vpn:devices_remove:{page}")])
    max_page = max(0, (total - 1) // page_size)
    if max_page:
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(InlineKeyboardButton(text="◀️", callback_data=f"vpn:devices:{page - 1}"))
        if page < max_page:
            navigation.append(InlineKeyboardButton(text="▶️", callback_data=f"vpn:devices:{page + 1}"))
        if navigation:
            rows.append(navigation)
    rows.append(_back("vpn:subscription"))
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(inline_keyboard=rows)


def device_removal_screen(
    devices: list[Dict[str, Any]], *, page: int, page_size: int = 5
) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [
            InlineKeyboardButton(
                text=_device_label(device, page * page_size + index),
                callback_data=f"vpn:device_remove:{int(device['id'])}:{page}",
            )
        ]
        for index, device in enumerate(devices, start=1)
    ]
    rows.append(_back(f"vpn:devices:{page}"))
    return (
        "🗑 <b>Отвязать устройство</b>\n\nВыберите устройство, которое хотите отвязать.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _referral_text(user: Dict[str, Any], stats: Any, bot_username: str) -> str:
    code = str(user.get("referral_code") or f"tg{user['telegram_id']}")
    username = bot_username or "your_vpn_bot"
    link = f"https://t.me/{username}?start=ref_{code}"
    minimum = format_rubles_from_kopecks(stats.withdrawal_min_kopecks).replace(" ₽", "₽")
    example_earned = int(1000 * stats.rate_percent / 100)
    return (
        f"👥 <b>Приглашайте друзей и зарабатывайте {stats.rate_percent}% с каждого пополнения!</b>\n\n"
        f"Например:\n<blockquote>— Друзья перешли по вашей ссылке и потратили 1000₽\n"
        f"— Вы получаете {example_earned}₽ и выводите на карту!</blockquote>\n\n"
        "📊 <b>Ваша статистика:</b>\n<blockquote>"
        f"— Приглашено: {stats.invited_count}\n"
        f"— Баланс: {escape(format_rubles_from_kopecks(stats.balance_kopecks))}\n"
        f"— Способ вывода: {escape(stats.withdrawal_method or 'не задан')}\n"
        f"— Реквизиты: {escape(stats.requisites or 'не указаны')}</blockquote>\n\n"
        f"% <b>Текущая ставка: {stats.rate_percent}%</b>\n💼 Вывод доступен от {minimum}\n\n"
        f"🔗 <b>Пригласительная ссылка:</b>\n<code>{escape(link)}</code>\n\n"
        "📨 Нажмите на ссылку, чтобы скопировать и поделиться с друзьями!"
    )


def create_vpn_router(services: AppServices) -> Router:
    router = Router(name="vpn")

    def render_subscription(
        subscription: Dict[str, Any] | None,
        *,
        user: Dict[str, Any] | None = None,
        balance_kopecks: int = 0,
        trial_available: bool = False,
    ) -> tuple[str, InlineKeyboardMarkup]:
        return subscription_screen(
            with_delivery_subscription(subscription, services.settings),
            support_username=services.settings.vpn_support_username,
            subscription_base_url=(
                delivery_base_url(services.settings)
                or services.settings.vpn_subscription_base_url
            ),
            user=user,
            balance_kopecks=balance_kopecks,
            trial_available=trial_available,
        )

    async def show_main(message: Message, *, user_id: int) -> None:
        trial_available = not services.vpn.has_used_trial(user_id)
        current_subscription = services.vpn.get_current_subscription(user_id)
        active_subscription = is_subscription_active(current_subscription)
        await _screen(
            message,
            main_screen_text(
                trial_available=trial_available,
                active_subscription=active_subscription,
            ),
            main_keyboard(
                support_username=services.settings.vpn_support_username,
                trial_available=trial_available,
                active_subscription=active_subscription,
            ),
        )

    async def show_plans(message: Message, *, user_id: int) -> None:
        active_sub = services.vpn.get_current_subscription(user_id)
        has_active_paid = (
            active_sub is not None
            and (
                str(active_sub.get("billing_kind")) == "paid"
                or active_sub.get("plan_id") is not None
                or str(active_sub.get("kind")) not in ("", "trial")
            )
        )
        discount = services.vpn.get_unapplied_discount(user_id)
        await _screen(
            message,
            "<b>Подключить VPN 🚀</b>\n\n"
            "Любой тариф предназначен для <b>2 устройств.</b>\n\n"
            "ℹ️ Выберите срок подписки",
            plans_keyboard(
                has_active_paid_subscription=has_active_paid,
                discount=discount,
            ),
        )

    async def show_subscription(message: Message, *, user: Dict[str, Any]) -> None:
        user_id = int(user["id"])
        current = services.vpn.get_current_subscription(user_id)
        referral_stats = services.referrals.stats(user_id)
        trial_available = not services.vpn.has_used_trial(user_id)
        text, kb = render_subscription(
            current,
            user=user,
            balance_kopecks=referral_stats.balance_kopecks,
            trial_available=trial_available,
        )
        await _screen(message, text, kb)

    async def show_about(message: Message) -> None:
        await _screen(
            message,
            "🛡 <b>О сервисе</b>\n\n"
            "CEA VPN — простой VPN для стабильного и защищённого подключения.\n\n"
            "Документы доступны по кнопкам ниже.\n\n"
            f"Канал — {escape(services.settings.vpn_channel_url)}\n"
            f"Поддержка — @{escape(services.settings.vpn_support_username)}",
            about_keyboard(services.settings),
        )

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        existing = services.users.get_by_telegram_id(message.from_user.id)
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        services.referrals.apply_start_referral(
            user_id=user["id"], start_text=message.text, user_was_registered=existing is not None
        )
        start_payload = (message.text or "").partition(" ")[2].strip().lower()
        if start_payload == "plans":
            await show_plans(message, user_id=int(user["id"]))
            return
        if start_payload == "subscription":
            await show_subscription(message, user=user)
            return
        if start_payload == "about":
            await show_about(message)
            return
        await show_main(message, user_id=int(user["id"]))

    @router.message(Command("menu"))
    async def menu(message: Message) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        await show_main(message, user_id=int(user["id"]))

    @router.callback_query(F.data == "vpn:main")
    async def main(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if callback.message:
            await show_main(callback.message, user_id=int(user["id"]))
        await callback.answer()

    @router.callback_query(F.data == "vpn:about")
    async def about(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if callback.message:
            await show_about(callback.message)
        await callback.answer()

    @router.callback_query(F.data == "vpn:promo")
    async def promo(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(VpnPromoState.waiting_for_code)
        if callback.message:
            await _screen(
                callback.message,
                "🎟 <b>Введите промокод одним сообщением:</b>",
                InlineKeyboardMarkup(inline_keyboard=[_back("vpn:about")]),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("vpn:promo:activate:"))
    async def activate_personal_promo(callback: CallbackQuery) -> None:
        code = callback.data.rsplit(":", 1)[-1]
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        try:
            await asyncio.to_thread(
                services.vpn.redeem_promocode,
                user_id=int(user["id"]),
                code=code,
            )
        except BusinessRuleError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if callback.message:
            await show_plans(callback.message, user_id=int(user["id"]))
        await callback.answer("Скидка применена!")

    @router.message(VpnPromoState.waiting_for_code)
    async def process_promo_code(message: Message, state: FSMContext) -> None:
        await state.clear()
        code = (message.text or "").strip()
        if not code:
            await message.answer(
                "❌ <b>Введите валидный промокод текстом.</b>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🎟 Попробовать ещё раз", callback_data="vpn:promo")],
                    _back("vpn:about"),
                ]),
                parse_mode="HTML",
            )
            return

        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        try:
            _, reward_summary = await asyncio.to_thread(
                services.vpn.redeem_promocode,
                user_id=int(user["id"]),
                code=code,
            )
        except BusinessRuleError as exc:
            await message.answer(
                f"❌ <b>Не удалось применить промокод:</b>\n{escape(str(exc))}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🎟 Попробовать ещё раз", callback_data="vpn:promo")],
                    _back("vpn:about"),
                ]),
                parse_mode="HTML",
            )
            return

        current = services.vpn.get_current_subscription(int(user["id"]))
        kb_buttons = []
        if current and current.get("subscription_url"):
            kb_buttons.append([InlineKeyboardButton(text="🚀 Мое подключение", callback_data="vpn:subscription")])
        kb_buttons.append(_back("vpn:about"))

        await message.answer(
            f"🎉 <b>Промокод «{escape(code.upper())}» успешно активирован!</b>\n\n"
            f"Вам начислено: <b>{escape(reward_summary)}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons),
            parse_mode="HTML",
        )

    @router.callback_query(F.data == "vpn:subscription")
    async def subscription(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if callback.message:
            await show_subscription(callback.message, user=user)
        await callback.answer()

    @router.callback_query(F.data.startswith("vpn:devices:"))
    async def connected_devices(callback: CallbackQuery) -> None:
        try:
            page = max(0, int((callback.data or "").rsplit(":", 1)[-1]))
        except ValueError:
            await callback.answer("Некорректный запрос.", show_alert=True)
            return
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        subscription, devices, total = services.vpn.list_subscription_devices(
            user_id=int(user["id"]), page=page
        )
        if callback.message:
            text, keyboard = connected_devices_screen(
                subscription, devices, total=total, page=page
            )
            await _screen(callback.message, text, keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("vpn:devices_remove:"))
    async def choose_device_to_remove(callback: CallbackQuery) -> None:
        try:
            page = max(0, int((callback.data or "").rsplit(":", 1)[-1]))
        except ValueError:
            await callback.answer("Некорректный запрос.", show_alert=True)
            return
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        _, devices, total = services.vpn.list_subscription_devices(
            user_id=int(user["id"]), page=page
        )
        if not total:
            await callback.answer("Подключённых устройств уже нет.", show_alert=True)
            return
        if callback.message:
            text, keyboard = device_removal_screen(devices, page=page)
            await _screen(callback.message, text, keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("vpn:device_remove:"))
    async def confirm_device_removal(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            await callback.answer("Некорректный запрос.", show_alert=True)
            return
        try:
            device_id, page = int(parts[2]), max(0, int(parts[3]))
        except ValueError:
            await callback.answer("Некорректный запрос.", show_alert=True)
            return
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        device = services.vpn.get_subscription_device(
            user_id=int(user["id"]), device_id=device_id
        )
        if device is None:
            await callback.answer("Устройство уже отвязано или недоступно.", show_alert=True)
            return
        name = escape(str(device.get("model") or "Не определено"))
        if callback.message:
            await _screen(
                callback.message,
                f"Вы уверены, что хотите отвязать устройство «{name}»?",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Отвязать", callback_data=f"vpn:device_remove_confirm:{device_id}:{page}")],
                        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"vpn:devices:{page}")],
                        _back(f"vpn:devices_remove:{page}"),
                    ]
                ),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("vpn:device_remove_confirm:"))
    async def remove_device(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            await callback.answer("Некорректный запрос.", show_alert=True)
            return
        try:
            device_id, page = int(parts[2]), max(0, int(parts[3]))
        except ValueError:
            await callback.answer("Некорректный запрос.", show_alert=True)
            return
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        removed = services.vpn.detach_subscription_device(
            user_id=int(user["id"]), device_id=device_id
        )
        if not removed:
            await callback.answer("Устройство уже отвязано или недоступно.", show_alert=True)
            return
        subscription, devices, total = services.vpn.list_subscription_devices(
            user_id=int(user["id"]), page=page
        )
        if callback.message:
            text, keyboard = connected_devices_screen(
                subscription, devices, total=total, page=page
            )
            await _screen(callback.message, text, keyboard)
        await callback.answer("Устройство отвязано. Слот освобождён.", show_alert=True)

    @router.callback_query(F.data == "vpn:trial")
    async def trial(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if services.vpn.has_used_trial(int(user["id"])):
            if callback.message:
                await _screen(
                    callback.message,
                    "🎁 <b>Пробный период уже использован</b>\n\n"
                    "Выберите тариф, чтобы подключить VPN.",
                    InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="🚀 Выбрать тариф",
                                    callback_data="vpn:plans",
                                )
                            ],
                            _back(),
                        ]
                    ),
                )
            await callback.answer("Пробный период уже использован.", show_alert=False)
            return

        if callback.message:
            channel = _channel_username(services.settings.vpn_channel_url)
            await _screen(
                callback.message,
                "🎁 <b>3 дня бесплатно</b>\n\n"
                f"Чтобы получить доступ, подпишитесь на канал {escape(channel)}.\n\n"
                "ℹ️ После подписки нажмите проверку",
                trial_keyboard(services.settings.vpn_channel_url),
            )
        await callback.answer()

    @router.callback_query(F.data == "vpn:trial_check")
    async def trial_check(callback: CallbackQuery) -> None:
        channel = _channel_username(services.settings.vpn_channel_url)
        try:
            member = await callback.bot.get_chat_member(
                chat_id=channel,
                user_id=callback.from_user.id,
            )
            subscribed = member.status not in {
                ChatMemberStatus.LEFT,
                ChatMemberStatus.KICKED,
            }
        except Exception:
            logging.exception("Could not verify VPN trial channel membership")
            subscribed = False
        if subscribed:
            user = services.users.ensure_telegram_user(**_user_kwargs(callback))
            try:
                outcome = services.vpn.claim_trial(
                    user_id=int(user["id"]),
                    channel=channel,
                )
            except BusinessRuleError as exc:
                await callback.answer(str(exc), show_alert=True)
                return
            sub = outcome.subscription
            trial_expired = outcome.trial_already_used and str(
                sub.get("status") or ""
            ) in {"expired", "disabled"}
            if callback.message:
                if trial_expired:
                    text = (
                        "🎁 <b>Пробный период уже использован</b>\n\n"
                        "Выберите тариф, чтобы снова подключить VPN."
                    )
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="🚀 Выбрать тариф",
                                    callback_data="vpn:plans",
                                )
                            ],
                            _back(),
                        ]
                    )
                else:
                    text, kb = render_subscription(sub, user=user)
                await _screen(callback.message, text, kb)
            if trial_expired:
                await callback.answer("Пробный период уже использован.", show_alert=False)
            else:
                await callback.answer("Подписка подтверждена — подключаем VPN.")
        else:
            await callback.answer(
                "Подписка не найдена. Подпишитесь на канал и попробуйте ещё раз.",
                show_alert=True,
            )

    @router.callback_query(F.data == "vpn:plans")
    async def plans(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if callback.message:
            await show_plans(callback.message, user_id=int(user["id"]))
        await callback.answer()

    @router.callback_query(F.data == "vpn:add_devices")
    async def add_devices(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        active_sub = services.vpn.get_current_subscription(user["id"])
        is_paid = (
            active_sub is not None
            and (
                str(active_sub.get("billing_kind")) == "paid"
                or active_sub.get("plan_id") is not None
                or str(active_sub.get("kind")) not in ("", "trial")
            )
        )
        if not is_paid:
            await callback.answer(
                "Докупка устройств доступна только при активной платной подписке.",
                show_alert=True,
            )
            return

        current_limit = int(active_sub.get("plan_max_devices") or 2)
        available = max(0, MAX_SUBSCRIPTION_DEVICES - current_limit)
        if available == 0:
            await callback.answer(
                "Достигнут максимальный лимит — 7 устройств.", show_alert=True
            )
            return

        if callback.message:
            await _screen(
                callback.message,
                "<b>Докупить устройства 📱</b>\n\n"
                "Выберите количество дополнительных устройств для подключения к вашему VPN:\n\n"
                f"Сейчас доступно: <b>{current_limit} из {MAX_SUBSCRIPTION_DEVICES}</b>.\n"
                "ℹ️ Новые устройства привязываются к вашей текущей подписке.",
                add_devices_keyboard(available),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("vpn:extra_dev:"))
    async def select_extra_dev(callback: CallbackQuery) -> None:
        try:
            cnt = int(callback.data.rsplit(":", 1)[-1])
        except ValueError:
            await callback.answer("Некорректное значение.", show_alert=True)
            return
        price_rub = EXTRA_DEVICE_PRICES.get(cnt)
        price_stars = EXTRA_DEVICE_STARS.get(cnt)
        if price_rub is None or price_stars is None:
            await callback.answer("Некорректное количество устройств.", show_alert=True)
            return

        if callback.message:
            await _screen(
                callback.message,
                "<b>Докупить устройства 📱</b>\n\n"
                f"Дополнительно устройств: <b>+{cnt} шт.</b>\n"
                f"К оплате: <b>{price_rub}₽ / {price_stars} ⭐</b>\n\n"
                "💡 Выберите способ оплаты:",
                extra_devices_payment_keyboard(cnt),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("vpn:pay_extra_dev:"))
    async def pay_extra_dev(callback: CallbackQuery) -> None:
        parts = callback.data.split(":", 3)
        if len(parts) != 4:
            await callback.answer("Некорректный запрос.", show_alert=True)
            return
        _, _, count_str, method = parts
        try:
            cnt = int(count_str)
        except ValueError:
            await callback.answer("Некорректное количество устройств.", show_alert=True)
            return
        price_rub = EXTRA_DEVICE_PRICES.get(cnt)
        price_stars = EXTRA_DEVICE_STARS.get(cnt)
        if price_rub is None or price_stars is None:
            await callback.answer("Некорректная сумма.", show_alert=True)
            return

        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        is_owner = _admin_demo_authorized(callback, services)

        if method == "stars":
            try:
                order = await asyncio.to_thread(
                    services.vpn.create_extra_devices_stars_payment,
                    user_id=int(user["id"]),
                    count=cnt,
                )
            except BusinessRuleError as exc:
                await callback.answer(str(exc), show_alert=True)
                return

            if callback.message:
                order_code = f"CEA-H{int(order['id']):05X}"
                await _screen(
                    callback.message,
                    f"📦 <b>Заказ: {order_code}</b>\n\n"
                    f"Услуга: <b>Доп. устройства (+{cnt} шт.)</b>\n"
                    "Оплата: <b>Telegram Stars (⭐)</b>\n"
                    f"Сумма: <b>{price_stars} ⭐</b>\n\n"
                    "💡 Оплатите заказ и нажмите проверку оплаты\n\n"
                    f"Нажмите «Заплатить ⭐️{price_stars}» ниже. После оплаты устройства добавятся автоматически.",
                    InlineKeyboardMarkup(
                        inline_keyboard=[_back("vpn:add_devices")]
                    ),
                )

            try:
                await callback.bot.send_invoice(
                    chat_id=callback.from_user.id,
                    title=f"Доп. устройства (+{cnt} шт.)",
                    description=f"Дополнительные устройства ({cnt} шт.) для CEA VPN",
                    payload=f"vpn_stars_{order['id']}",
                    currency="XTR",
                    prices=[LabeledPrice(label=f"+{cnt} устр.", amount=price_stars)],
                    provider_token="",
                )
            except Exception:
                logging.exception("Could not send Telegram Stars invoice for extra devices")
                await callback.answer("Не удалось выставить счёт в Telegram Stars.", show_alert=True)
                return
            await callback.answer()
            return

        if services.vpn.uses_platega:
            try:
                order, _ = await asyncio.to_thread(
                    services.vpn.create_extra_devices_platega_payment,
                    user_id=int(user["id"]),
                    count=cnt,
                    user_name=callback.from_user.username or "",
                )
            except BusinessRuleError as exc:
                await callback.answer(str(exc), show_alert=True)
                return
            payment_url = str(order.get("payment_url") or "")
            if not payment_url:
                await callback.answer("Ссылка на оплату ещё создаётся. Нажмите еще раз.", show_alert=True)
                return

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Оплатить картой / СБП", url=payment_url)],
                _back("vpn:add_devices"),
            ])
            order_code = f"CEA-H{int(order['id']):05X}"
            if callback.message:
                await _screen(
                    callback.message,
                    f"📦 <b>Заказ: {order_code}</b>\n\n"
                    f"Услуга: <b>Доп. устройства (+{cnt} шт.)</b>\n"
                    "Оплата: <b>Карта / СБП</b>\n"
                    f"Сумма: <b>{price_rub}₽</b>\n\n"
                    "ℹ️ Оплатите заказ — подтверждение придёт автоматически.\n\n"
                    "После оплаты дополнительные устройства добавятся к вашей подписке автоматически.",
                    kb,
                )
            await callback.answer()
            return

        if is_owner:
            try:
                order, _ = services.vpn.create_extra_devices_admin_demo_payment(
                    user_id=int(user["id"]),
                    count=cnt,
                    admin_authorized=is_owner,
                )
            except BusinessRuleError as exc:
                await callback.answer(str(exc), show_alert=True)
                return
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🧪 Имитировать успешную оплату", callback_data=f"vpn:demo_pay:{order['id']}")],
                [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"vpn:check:{order['id']}")],
                _back("vpn:add_devices"),
            ])
            if callback.message:
                await _screen(
                    callback.message,
                    f"📦 <b>Тестовый заказ: CEA-TEST-{int(order['id']):06d}</b>\n\n"
                    f"Услуга: <b>Доп. устройства (+{cnt} шт.)</b>\n"
                    f"Сумма: <b>{price}₽</b>\n\n"
                    "ℹ️ Личный тестовый режим владельца\n\n"
                    "Устройства добавятся после имитации успешной оплаты.",
                    kb,
                )
            await callback.answer()
            return

        await callback.answer("Оплата временно недоступна.", show_alert=True)

    @router.callback_query(F.data.startswith("vpn:tariff:"))
    async def tariff(callback: CallbackQuery) -> None:
        code = callback.data.rsplit(":", 1)[-1]
        tariff_data = TARIFFS.get(code)
        if tariff_data is None:
            await callback.answer("Тариф не найден.", show_alert=True)
            return
        name, price_rub, price_stars = tariff_data
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        discount = await asyncio.to_thread(
            services.vpn.get_unapplied_discount, int(user["id"])
        )
        price_rub, price_stars = _discounted_prices(
            price_rub, price_stars, discount
        )
        if callback.message:
            await _screen(
                callback.message,
                "Покупка VPN\n\n"
                f"Тариф: <b>{name}</b>\n"
                "Доступно: <b>2 устройства</b>\n"
                f"К оплате: <b>{price_rub}₽ / {price_stars} ⭐</b>\n\n"
                "💡 Выберите способ оплаты:",
                payment_keyboard(code),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("vpn:payment:"))
    async def payment(callback: CallbackQuery) -> None:
        parts = callback.data.split(":", 3)
        if len(parts) != 4:
            await callback.answer("Некорректный заказ.", show_alert=True)
            return
        _, _, code, method = parts
        tariff_data = TARIFFS.get(code)
        plan_code = VPN_PLAN_CODES.get(code)
        if tariff_data is None or plan_code is None:
            await callback.answer("Тариф не найден.", show_alert=True)
            return
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        name, base_price_rub, base_price_stars = tariff_data
        discount = await asyncio.to_thread(
            services.vpn.get_unapplied_discount, int(user["id"])
        )
        price_rub, price_stars = _discounted_prices(
            base_price_rub, base_price_stars, discount
        )

        if method == "stars":
            try:
                order = await asyncio.to_thread(
                    services.vpn.create_stars_payment,
                    user_id=int(user["id"]),
                    plan_code=plan_code,
                )
            except BusinessRuleError as exc:
                await callback.answer(str(exc), show_alert=True)
                return

            if callback.message:
                order_code = f"CEA-H{int(order['id']):05X}"
                await _screen(
                    callback.message,
                    f"📦 <b>Заказ: {order_code}</b>\n\n"
                    f"VPN: <b>{name}</b>\n"
                    "Доступно: <b>до 2 устройств</b>\n"
                    "Оплата: <b>Telegram Stars (⭐)</b>\n"
                    f"Сумма: <b>{price_stars} ⭐</b>\n\n"
                    "💡 Оплатите заказ и нажмите проверку оплаты\n\n"
                    f"Нажмите «Заплатить ⭐️{price_stars}» ниже. После оплаты подписка выдастся автоматически.",
                    InlineKeyboardMarkup(
                        inline_keyboard=[_back(f"vpn:tariff:{code}")]
                    ),
                )

            try:
                await callback.bot.send_invoice(
                    chat_id=callback.from_user.id,
                    title=f"VPN {name}",
                    description=f"Подписка VPN на {name} — 2 устройства",
                    payload=f"vpn_stars_{order['id']}",
                    currency="XTR",
                    prices=[LabeledPrice(label=f"VPN {name}", amount=price_stars)],
                    provider_token="",
                )
            except Exception:
                logging.exception("Could not send Telegram Stars invoice for VPN")
                await callback.answer("Не удалось выставить счёт в Telegram Stars.", show_alert=True)
                return
            await callback.answer()
            return

        labels = {"platega": "Карта / СБП"}
        if method in LEGACY_PAYMENT_METHODS:
            if callback.message:
                await _screen(
                    callback.message,
                    "Покупка VPN\n\n"
                    f"Тариф: <b>{name}</b>\n"
                    "Доступно: <b>2 устройства</b>\n"
                    f"К оплате: <b>{price_rub}₽ / {price_stars} ⭐</b>\n\n"
                    "ℹ️ Способы оплаты обновились. "
                    "Выберите оплату через Platega или Звёзды.",
                    payment_keyboard(code),
                )
            await callback.answer("Выберите новый способ оплаты.", show_alert=True)
            return
        if method not in labels:
            await callback.answer("Способ оплаты не найден.", show_alert=True)
            return

        is_owner = _admin_demo_authorized(callback, services)
        if services.vpn.uses_platega:
            try:
                order, _ = await asyncio.to_thread(
                    services.vpn.create_platega_payment,
                    user_id=int(user["id"]),
                    plan_code=plan_code,
                    user_name=callback.from_user.username or "",
                )
            except BusinessRuleError as exc:
                await callback.answer(str(exc), show_alert=True)
                return
            payment_url = str(order.get("payment_url") or "")
            if not payment_url:
                await callback.answer(
                    "Ссылка на оплату ещё создаётся. Нажмите ещё раз.",
                    show_alert=True,
                )
                return
            if callback.message:
                order_code = f"CEA-H{int(order['id']):05X}"
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="💳 Оплатить картой / СБП",
                                url=payment_url,
                            )
                        ],
                        _back(f"vpn:tariff:{code}"),
                    ]
                )
                await _screen(
                    callback.message,
                    f"📦 <b>Заказ: {order_code}</b>\n\n"
                    f"VPN: <b>{name}</b>\n"
                    "Доступно: <b>до 1 устройства</b>\n"
                    "Оплата: <b>Карта / СБП</b>\n"
                    f"Сумма: <b>{int(order['amount_rub'])}₽</b>\n\n"
                    "ℹ️ Нажмите «Оплатить картой / СБП». После оплаты бот сам "
                    "пришлёт подтверждение и экран «Моя подписка».",
                    kb,
                )
            await callback.answer()
            return

        if not is_owner:
            if callback.message:
                await _screen(
                    callback.message,
                    "💳 <b>Оплата временно недоступна</b>\n\n"
                    "Попробуйте ещё раз чуть позже. "
                    "Без подтверждённой оплаты VPN-ключ не создаётся.",
                    InlineKeyboardMarkup(
                        inline_keyboard=[_back(f"vpn:tariff:{code}")]
                    ),
                )
            await callback.answer()
            return
        try:
            order, _ = services.vpn.create_admin_demo_payment(
                user_id=int(user["id"]),
                plan_code=plan_code,
                payment_method="other",
                admin_authorized=is_owner,
            )
        except BusinessRuleError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if callback.message:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🧪 Имитировать успешную оплату", callback_data=f"vpn:demo_pay:{order['id']}")],
                [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"vpn:check:{order['id']}")],
                _back(f"vpn:tariff:{code}"),
            ])
            await _screen(
                callback.message,
                f"📦 <b>Тестовый заказ: CEA-TEST-{int(order['id']):06d}</b>\n\n"
                f"VPN: <b>{name}</b>\n"
                "Доступно: <b>2 устройства</b>\n"
                f"Оплата: <b>{labels[method]}</b>\n"
                f"Сумма: <b>{int(order['amount_rub'])}₽</b>\n\n"
                "ℹ️ Личный тестовый режим владельца\n\n"
                "Ключ будет создан только после имитации успешной оплаты. "
                "Деньги не списываются.",
                kb,
            )
        await callback.answer()

    @router.pre_checkout_query(F.invoice_payload.startswith("vpn_stars_"))
    async def vpn_stars_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
        await pre_checkout_query.answer(ok=True)

    @router.message(F.successful_payment, F.successful_payment.invoice_payload.startswith("vpn_stars_"))
    async def vpn_stars_successful_payment(message: Message) -> None:
        payload = message.successful_payment.invoice_payload
        raw_id = payload.removeprefix("vpn_stars_")
        if not raw_id.isdigit():
            return
        payment_id = int(raw_id)
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        outcome = await asyncio.to_thread(
            services.vpn.confirm_stars_payment,
            user_id=int(user["id"]),
            payment_id=payment_id,
            telegram_payment_charge_id=message.successful_payment.telegram_payment_charge_id,
        )
        text, kb = render_subscription(outcome.subscription, user=user)
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

    @router.callback_query(F.data.startswith("vpn:demo_pay:"))
    async def confirm_demo_payment(callback: CallbackQuery) -> None:
        payment_id = _payment_callback_id(callback.data, "vpn:demo_pay")
        if payment_id is None:
            await callback.answer("Некорректный заказ.", show_alert=True)
            return
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        try:
            outcome = services.vpn.confirm_admin_demo_payment(
                user_id=int(user["id"]),
                payment_id=payment_id,
                admin_authorized=_admin_demo_authorized(callback, services),
            )
        except BusinessRuleError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if not outcome.processed:
            await callback.answer("Этот тестовый заказ уже подтверждён.")
            return
        if callback.message:
            text, kb = render_subscription(
                outcome.subscription,
                user=user,
            )
            await _screen(callback.message, text, kb)
        await callback.answer("Тестовая оплата подтверждена — подключаем VPN.")

    @router.callback_query(F.data.startswith("vpn:check:"))
    async def check_payment(callback: CallbackQuery) -> None:
        payment_id = _payment_callback_id(callback.data, "vpn:check")
        if payment_id is None:
            await callback.answer("Некорректный заказ.", show_alert=True)
            return
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        payment_record = services.vpn.get_payment_for_user(
            user_id=int(user["id"]),
            payment_id=payment_id,
        )
        if payment_record is None:
            await callback.answer("Заказ не найден.", show_alert=True)
            return
        if payment_record.get("provider") == "platega":
            try:
                outcome = await asyncio.to_thread(
                    services.vpn.check_platega_payment,
                    user_id=int(user["id"]),
                    payment_id=payment_id,
                )
            except BusinessRuleError as exc:
                await callback.answer(str(exc), show_alert=True)
                return
            if outcome.status == "pending":
                await callback.answer(
                    "Оплата ещё не подтверждена. Если вы только что оплатили, "
                    "подождите несколько секунд.",
                    show_alert=True,
                )
                return
            if outcome.status in {"cancelled", "failed"}:
                await callback.answer(
                    "Этот платёж отменён. Создайте новый заказ.",
                    show_alert=True,
                )
                return
            if outcome.status == "refunded":
                await callback.answer(
                    "Платёж возвращён. Напишите в поддержку.",
                    show_alert=True,
                )
                return
            if not outcome.confirmed or outcome.subscription is None:
                await callback.answer(
                    "Оплата пока не подтверждена. Ключ не создан.",
                    show_alert=True,
                )
                return
            if callback.message:
                text, kb = render_subscription(
                    outcome.subscription,
                    user=user,
                )
                await _screen(callback.message, text, kb)
            await callback.answer(
                "Оплата подтверждена — подключаем VPN."
                if outcome.processed
                else "Оплата уже подтверждена."
            )
            return

        if not _admin_demo_authorized(callback, services):
            await callback.answer(
                "Тестовая оплата доступна только владельцу бота.",
                show_alert=True,
            )
            return
        if payment_record.get("status") != "paid":
            await callback.answer(
                "Оплата ещё не подтверждена. Ключ не создан.",
                show_alert=True,
            )
            return
        current = services.vpn.get_payment_subscription_for_user(
            user_id=int(user["id"]),
            payment_id=payment_id,
        )
        if current is None:
            await callback.answer(
                "Оплата подтверждена, но подписка ещё не создана.",
                show_alert=True,
            )
            return
        if callback.message:
            text, kb = render_subscription(
                current,
                user=user,
            )
            await _screen(callback.message, text, kb)
        await callback.answer("Оплата подтверждена.")

    @router.callback_query(F.data.in_({"vpn:demo_pay", "vpn:check"}))
    async def stale_demo_notice(callback: CallbackQuery) -> None:
        await callback.answer("Этот тестовый заказ устарел. Создайте новый.", show_alert=True)

    @router.callback_query(F.data == "vpn:earn")
    async def earn(callback: CallbackQuery) -> None:
        await callback.answer(
            "🛠 Раздел «Заработать» временно недоступен. Ведутся технические работы.",
            show_alert=True,
        )

    @router.callback_query(F.data == "vpn:withdraw")
    async def withdraw(callback: CallbackQuery) -> None:
        await callback.answer(
            "🛠 Раздел «Заработать» временно недоступен. Ведутся технические работы.",
            show_alert=True,
        )

    return router


create_router = create_vpn_router
