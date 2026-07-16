from __future__ import annotations

import logging
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
    Message,
)

from ceai.services.app import AppServices
from ceai.services.exceptions import BusinessRuleError
from ceai.services.referrals import format_rubles_from_kopecks


TARIFFS = {
    "1": ("1 месяц", 189, 149),
    "3": ("3 месяца", 479, 399),
    "6": ("6 месяцев", 790, 649),
    "12": ("1 год", 1290, 999),
}

VPN_PLAN_CODES = {
    "1": "vpn-1m",
    "3": "vpn-3m",
    "6": "vpn-6m",
    "12": "vpn-12m",
}


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
    except Exception:
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


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


def main_keyboard(*, support_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 3 дня бесплатно", callback_data="vpn:trial", style="success")],
        [InlineKeyboardButton(text="Подключить VPN 🚀", callback_data="vpn:plans", style="primary")],
        [InlineKeyboardButton(text="👤 Моя подписка", callback_data="vpn:subscription")],
        [InlineKeyboardButton(text="🥷 Заработать", callback_data="vpn:earn")],
        [
            InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{support_username}"),
            InlineKeyboardButton(text="🛡 О сервисе", callback_data="vpn:about"),
        ],
    ])


def plans_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{name} – {old}₽ / {price} ⭐", callback_data=f"vpn:tariff:{code}")]
        for code, (name, old, price) in TARIFFS.items()
    ]
    rows.append(_back())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 СБП", callback_data=f"vpn:payment:{code}:sbp")],
        [InlineKeyboardButton(text="🇷🇺 Карта", callback_data=f"vpn:payment:{code}:card")],
        [InlineKeyboardButton(text="🔽 CryptoBot", callback_data=f"vpn:payment:{code}:crypto")],
        [InlineKeyboardButton(text="🌟 Telegram Stars", callback_data=f"vpn:payment:{code}:stars")],
        [InlineKeyboardButton(text="🌐 Другие способы", callback_data=f"vpn:payment:{code}:other")],
        _back("vpn:plans"),
    ])


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
    value = channel_url.strip().rstrip("/")
    if value.startswith("@"):
        return value
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
    return bool(
        services.settings.vpn_allow_admin_demo_payment
        and event.from_user.id in services.settings.vpn_admin_demo_telegram_ids
    )


def _format_ends_at(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y в %H:%M")


def subscription_screen(
    subscription: Dict[str, Any] | None,
    *,
    support_username: str,
    subscription_base_url: str,
) -> tuple[str, InlineKeyboardMarkup]:
    if subscription is None or subscription.get("status") in {"expired", "disabled"}:
        return (
            "👤 <b>Моя подписка</b>\n\n"
            "Статус: ❌ <b>Нет активной подписки</b>\n\n"
            "Подключите бесплатные 3 дня или выберите тариф.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🎁 3 дня бесплатно",
                            callback_data="vpn:trial",
                            style="success",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🚀 Подключить VPN",
                            callback_data="vpn:plans",
                        )
                    ],
                    _back(),
                ]
            ),
        )

    status = str(subscription.get("status") or "")
    if status in {"provisioning", "error"}:
        text = (
            "👤 <b>Моя подписка</b>\n\n"
            "Статус: ⏳ <b>Подключаем VPN</b>\n\n"
            "Сервер создаёт персональные настройки. Обычно это занимает "
            "до одной минуты; бот пришлёт их автоматически."
        )
        return text, InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Проверить подключение",
                        callback_data="vpn:subscription",
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
        )

    plan_name = subscription.get("plan_name") or "3 бесплатных дня"
    region = subscription.get("server_region") or "NL"
    ends_at = _format_ends_at(subscription["ends_at"])
    rows: list[list[InlineKeyboardButton]] = []
    subscription_url = str(subscription.get("subscription_url") or "")
    if happ_landing_url(subscription_url, subscription_base_url):
        rows.append(
            [
                subscription_open_button(subscription_url, subscription_base_url)
            ]
        )
        rows.append(
            [
                subscription_v2box_button(
                    subscription_url, subscription_base_url
                )
            ]
        )
        rows.append(
            [
                subscription_copy_button(subscription_url)
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="🆘 Поддержка",
                    url=f"https://t.me/{support_username}",
                )
            ],
            _back(),
        ]
    )
    return (
        "👤 <b>Моя подписка</b>\n\n"
        "Статус: ✅ <b>Активна</b>\n"
        f"Тариф: <b>{escape(str(plan_name))}</b>\n"
        f"Сервер: <b>{escape(str(region))}</b>\n"
        f"Действует до: <b>{escape(ends_at)} МСК</b>\n\n"
        "Персональную ссылку никому не передавайте.\n\n"
        f"{happ_subscription_instructions()}",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _referral_text(user: Dict[str, Any], stats: Any, bot_username: str) -> str:
    code = str(user.get("referral_code") or f"tg{user['telegram_id']}")
    username = bot_username or "your_vpn_bot"
    link = f"https://t.me/{username}?start=ref_{code}"
    minimum = format_rubles_from_kopecks(stats.withdrawal_min_kopecks).replace(" ₽", "₽")
    return (
        f"👥 <b>Приглашайте друзей и зарабатывайте {stats.rate_percent}% с каждого пополнения!</b>\n\n"
        "Например:\n<blockquote>— Друзья перешли по вашей ссылке и потратили 1000₽\n"
        "— Вы получаете 300.0₽ и выводите на КАРТУ!</blockquote>\n\n"
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

    async def show_main(message: Message) -> None:
        await _screen(
            message,
            "<b>Привет! Я — CEA VPN 🥷</b>\n\nПомогу подключить VPN за пару минут.\n\n"
            "⚡ Быстрое подключение\n🛡 Защищённое соединение\n🌍 Доступ к нужным сайтам",
            main_keyboard(support_username=services.settings.vpn_support_username),
        )

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        existing = services.users.get_by_telegram_id(message.from_user.id)
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        services.referrals.apply_start_referral(
            user_id=user["id"], start_text=message.text, user_was_registered=existing is not None
        )
        await show_main(message)

    @router.message(Command("menu"))
    async def menu(message: Message) -> None:
        services.users.ensure_telegram_user(**_user_kwargs(message))
        await show_main(message)

    @router.callback_query(F.data == "vpn:main")
    async def main(callback: CallbackQuery) -> None:
        if callback.message:
            await show_main(callback.message)
        await callback.answer()

    @router.callback_query(F.data == "vpn:about")
    async def about(callback: CallbackQuery) -> None:
        if callback.message:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📄 Публичная оферта", url=services.settings.public_offer_url),
                 InlineKeyboardButton(text="🔒 Политика конфиденциальности", url=services.settings.privacy_policy_url)],
                [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="vpn:promo")],
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
        if callback.message:
            text, kb = subscription_screen(
                current,
                support_username=services.settings.vpn_support_username,
                subscription_base_url=services.settings.vpn_subscription_base_url,
            )
            await _screen(callback.message, text, kb)
        await callback.answer()

    @router.callback_query(F.data == "vpn:trial")
    async def trial(callback: CallbackQuery) -> None:
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
            current = services.vpn.get_current_subscription(int(user["id"]))
            if callback.message:
                text, kb = subscription_screen(
                    current or outcome.subscription,
                    support_username=services.settings.vpn_support_username,
                    subscription_base_url=services.settings.vpn_subscription_base_url,
                )
                if outcome.trial_already_used and (current or outcome.subscription).get(
                    "status"
                ) in {"expired", "disabled"}:
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
                await _screen(callback.message, text, kb)
            await callback.answer("Подписка подтверждена — подключаем VPN.")
        else:
            await callback.answer(
                "Подписка не найдена. Подпишитесь на канал и попробуйте ещё раз.",
                show_alert=True,
            )

    @router.callback_query(F.data == "vpn:plans")
    async def plans(callback: CallbackQuery) -> None:
        if callback.message:
            await _screen(callback.message, "<b>Подключить VPN 🚀</b>\n\nЛюбой тариф включает до <b>3 устройств.</b>\n\n<blockquote>▶ Выберите срок подписки</blockquote>", plans_keyboard())
        await callback.answer()

    @router.callback_query(F.data.startswith("vpn:tariff:"))
    async def tariff(callback: CallbackQuery) -> None:
        code = callback.data.rsplit(":", 1)[-1]
        tariff_data = TARIFFS.get(code)
        if tariff_data is None:
            await callback.answer("Тариф не найден.", show_alert=True)
            return
        name, old, price = tariff_data
        if callback.message:
            await _screen(callback.message, f"Покупка VPN\n\nТариф: <b>{name}</b>\nДоступно: <b>до 3 устройств</b>\nК оплате: <b>{old}₽ / {price} ⭐</b>\n\n<blockquote>▶ Выбери способ оплаты</blockquote>", payment_keyboard(code))
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
        name, _, _ = tariff_data
        labels = {"sbp": "СБП", "card": "Карта", "crypto": "CryptoBot", "stars": "Telegram Stars", "other": "Другие способы"}
        if method not in labels:
            await callback.answer("Способ оплаты не найден.", show_alert=True)
            return
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        is_owner = _admin_demo_authorized(callback, services)
        if not is_owner:
            if callback.message:
                await _screen(
                    callback.message,
                    "💳 <b>Оплата пока подключается</b>\n\n"
                    "Платёжные способы ещё закрыты для пользователей. "
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
                payment_method=method,
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
                "Доступно: <b>до 3 устройств</b>\n"
                f"Оплата: <b>{labels[method]}</b>\n"
                f"Сумма: <b>{int(order['amount_rub'])}₽</b>\n\n"
                "<blockquote>🧪 Личный тестовый режим владельца</blockquote>\n\n"
                "Ключ будет создан только после имитации успешной оплаты. "
                "Деньги не списываются.",
                kb,
            )
        await callback.answer()

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
            text, kb = subscription_screen(
                outcome.subscription,
                support_username=services.settings.vpn_support_username,
                subscription_base_url=services.settings.vpn_subscription_base_url,
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
        if not _admin_demo_authorized(callback, services):
            await callback.answer(
                "Тестовая оплата доступна только владельцу бота.",
                show_alert=True,
            )
            return
        payment_record = services.vpn.get_payment_for_user(
            user_id=int(user["id"]),
            payment_id=payment_id,
        )
        if payment_record is None:
            await callback.answer("Заказ не найден.", show_alert=True)
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
            text, kb = subscription_screen(
                current,
                support_username=services.settings.vpn_support_username,
                subscription_base_url=services.settings.vpn_subscription_base_url,
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
