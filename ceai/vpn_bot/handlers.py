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
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from ceai.services.app import AppServices
from ceai.services.exceptions import BusinessRuleError
from ceai.services.referrals import format_rubles_from_kopecks
from ceai.vpn_subscription_delivery import (
    delivery_base_url,
    with_delivery_subscription,
)


# (name, display_price_rub, stars_price) — stars_price kept for future use
TARIFFS = {
    "1": ("1 месяц", 199, 169),
    "3": ("3 месяца", 499, 419),
    "6": ("6 месяцев", 899, 759),
    "12": ("1 год", 1390, 1190),
}

VPN_PLAN_CODES = {
    "1": "vpn-1m",
    "3": "vpn-3m",
    "6": "vpn-6m",
    "12": "vpn-12m",
}

LEGACY_PAYMENT_METHODS = frozenset({"sbp", "card", "crypto", "other"})


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
    try:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as _exc:
        # MessageNotModified is harmless; other errors fall back to a new message.
        _msg = str(_exc).lower()
        if "message is not modified" not in _msg:
            logging.debug("edit_text failed, falling back to answer: %s", _exc)
        try:
            await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            logging.exception("_screen answer fallback also failed")


def _back(callback_data: str = "vpn:main") -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)]


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
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if trial_available:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🎁 3 дня бесплатно",
                    callback_data="vpn:trial",
                    style="success",
                )
            ]
        )
    rows.extend([
        [InlineKeyboardButton(text="Подключить VPN 🚀", callback_data="vpn:plans", style="primary")],
        [InlineKeyboardButton(text="👤 Моя подписка", callback_data="vpn:subscription")],
        [InlineKeyboardButton(text="🥷 Заработать", callback_data="vpn:earn")],
        [
            InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{support_username}"),
            InlineKeyboardButton(text="🛡 О сервисе", callback_data="vpn:about"),
        ],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plans_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{name} — {price_rub}₽ / {price_stars} ⭐️",
                callback_data=f"vpn:tariff:{code}",
            )
        ]
        for code, (name, price_rub, price_stars) in TARIFFS.items()
    ]
    rows.append(_back())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
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
            _back("vpn:plans"),
        ]
    )


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
        "Устройств: 1"
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
    raw_plan_name = str(subscription.get("plan_name") or "")
    if kind == "trial" or raw_plan_name in {"3 бесплатных дня", "Пробная подписка"}:
        plan_name = "Пробная подписка"
    else:
        plan_name = raw_plan_name or "30 дней"

    max_devices = int(subscription.get("plan_max_devices") or 1)
    ends_at = _format_ends_at(subscription["ends_at"])
    subscription_url = str(subscription.get("subscription_url") or "")

    sub_info_block = (
        "<blockquote>"
        f"💎 Тариф: {escape(plan_name)}\n"
        f"📱 Лимит устройств: {max_devices}\n"
        f"📅 Срок действия: {escape(ends_at)} (МСК)"
        "</blockquote>"
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
        [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{support_username}")]
    )
    rows.append(_back())

    return (
        f"👤 <b>Моя подписка:</b>\n\n"
        f"{user_info_block}\n\n"
        f"{sub_info_block}\n\n"
        f"{footer_text}",
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
        await _screen(
            message,
            "Приветствую в <b>CEA VPN</b> 🤗\n\n"
            "Здесь ты сможешь подключить VPN за пару минут.\n\n"
            "💎 Безлимитный трафик\n"
            "🚀 Быстрое подключение\n"
            "🔗 Доступ к заблокированным сайтам",
            main_keyboard(
                support_username=services.settings.vpn_support_username,
                trial_available=trial_available,
            ),
        )

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        existing = services.users.get_by_telegram_id(message.from_user.id)
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        services.referrals.apply_start_referral(
            user_id=user["id"], start_text=message.text, user_was_registered=existing is not None
        )
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
    async def about(callback: CallbackQuery) -> None:
        if callback.message:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📄 Публичная оферта", url=services.settings.public_offer_url),
                 InlineKeyboardButton(text="🔒 Политика конфиденциальности", url=services.settings.privacy_policy_url)],
                # Promo codes not yet implemented — button hidden until ready.
                [InlineKeyboardButton(text="🆘 Написать в поддержку", url=f"https://t.me/{services.settings.vpn_support_username}")],
                _back(),
            ])
            await _screen(callback.message, "🛡 <b>О сервисе</b>\n\nCEA VPN — простой VPN для стабильного и защищённого подключения.\n\nДокументы доступны по кнопкам ниже.\n\n"
                          f"Канал — {escape(services.settings.vpn_channel_url)}\nПоддержка — @{escape(services.settings.vpn_support_username)}", kb)
        await callback.answer()

    @router.callback_query(F.data == "vpn:promo")
    async def promo(callback: CallbackQuery) -> None:
        await callback.answer("Промокоды будут подключены на следующем этапе.", show_alert=True)

    @router.callback_query(F.data == "vpn:subscription")
    async def subscription(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        current = services.vpn.get_current_subscription(int(user["id"]))
        referral_stats = services.referrals.stats(int(user["id"]))
        trial_available = not services.vpn.has_used_trial(int(user["id"]))
        if callback.message:
            text, kb = render_subscription(
                current,
                user=user,
                balance_kopecks=referral_stats.balance_kopecks,
                trial_available=trial_available,
            )
            await _screen(callback.message, text, kb)
        await callback.answer()

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
                "<blockquote>▶ После подписки нажмите проверку</blockquote>",
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
        if callback.message:
            await _screen(
                callback.message,
                "<b>Подключить VPN 🚀</b>\n\n"
                "Любой тариф предназначен для <b>1 устройства.</b>\n\n"
                "<blockquote>▶ Выберите срок подписки</blockquote>",
                plans_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("vpn:tariff:"))
    async def tariff(callback: CallbackQuery) -> None:
        code = callback.data.rsplit(":", 1)[-1]
        tariff_data = TARIFFS.get(code)
        if tariff_data is None:
            await callback.answer("Тариф не найден.", show_alert=True)
            return
        name, price_rub, price_stars = tariff_data
        if callback.message:
            await _screen(
                callback.message,
                "Покупка VPN\n\n"
                f"Тариф: <b>{name}</b>\n"
                "Доступно: <b>1 устройство</b>\n"
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
        name, price_rub, price_stars = tariff_data
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))

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

            try:
                await callback.bot.send_invoice(
                    chat_id=callback.from_user.id,
                    title=f"VPN {name}",
                    description=f"Подписка VPN на {name} — 1 устройство",
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
                    "Доступно: <b>1 устройство</b>\n"
                    f"К оплате: <b>{price_rub}₽ / {price_stars} ⭐</b>\n\n"
                    "<blockquote>▶ Способы оплаты обновились. "
                    "Выберите оплату через Platega или Звёзды.</blockquote>",
                    payment_keyboard(code),
                )
            await callback.answer("Выберите новый способ оплаты.", show_alert=True)
            return
        if method not in labels:
            await callback.answer("Способ оплаты не найден.", show_alert=True)
            return

        if method == "platega":
            if callback.message:
                await _screen(
                    callback.message,
                    "💳 <b>Оплата картой / СБП</b>\n\n"
                    "Оплата картой временно недоступна. Воспользуйтесь оплатой <b>⭐ Звёздами</b>.",
                    InlineKeyboardMarkup(
                        inline_keyboard=[_back(f"vpn:tariff:{code}")]
                    ),
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
                "Доступно: <b>1 устройство</b>\n"
                f"Оплата: <b>{labels[method]}</b>\n"
                f"Сумма: <b>{int(order['amount_rub'])}₽</b>\n\n"
                "<blockquote>🧪 Личный тестовый режим владельца</blockquote>\n\n"
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
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        stats = services.referrals.stats(user["id"])
        if callback.message:
            await _screen(callback.message, _referral_text(user, stats, services.settings.vpn_bot_username), referral_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "vpn:withdraw")
    async def withdraw(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        stats = services.referrals.stats(user["id"])
        minimum = format_rubles_from_kopecks(stats.withdrawal_min_kopecks).replace(" ₽", "₽")
        if stats.balance_kopecks < stats.withdrawal_min_kopecks:
            await callback.answer(f"Вывод доступен от {minimum}.", show_alert=True)
        else:
            await callback.answer(f"Для вывода напишите @{services.settings.vpn_support_username}.", show_alert=True)

    return router
