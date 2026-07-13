from __future__ import annotations

from html import escape
from typing import Any, Dict

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ceai.services.app import AppServices
from ceai.services.referrals import format_rubles_from_kopecks


TARIFFS = {
    "1": ("1 месяц", 189, 149),
    "3": ("3 месяца", 479, 399),
    "6": ("6 месяцев", 790, 649),
    "12": ("1 год", 1290, 999),
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
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="vpn:trial_check", style="success")],
        _back(),
    ])


def _channel_username(channel_url: str) -> str:
    value = channel_url.strip().rstrip("/")
    if value.startswith("@"):
        return value
    if "t.me/" in value:
        return "@" + value.rsplit("/", 1)[-1].lstrip("@")
    return value


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
                 InlineKeyboardButton(text="🔒 Политика конфиденциальности", url=services.settings.public_offer_url)],
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
        if callback.message:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Подключить VPN", callback_data="vpn:plans")], _back()
            ])
            await _screen(callback.message, "👤 <b>Моя подписка</b>\n\nСтатус: ❌ <b>Нет активной подписки</b>\n\nПодключите бесплатные 3 дня или выберите тариф.", kb)
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
            subscribed = False
        if subscribed:
            await callback.answer(
                "Подписка подтверждена! Выдачу VPN подключим на следующем этапе.",
                show_alert=True,
            )
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
        name, old, price = TARIFFS.get(code, TARIFFS["1"])
        if callback.message:
            await _screen(callback.message, f"Покупка VPN\n\nТариф: <b>{name}</b>\nДоступно: <b>до 3 устройств</b>\nК оплате: <b>{old}₽ / {price} ⭐</b>\n\n<blockquote>▶ Выбери способ оплаты</blockquote>", payment_keyboard(code))
        await callback.answer()

    @router.callback_query(F.data.startswith("vpn:payment:"))
    async def payment(callback: CallbackQuery) -> None:
        _, _, code, method = callback.data.split(":", 3)
        name, old, _ = TARIFFS.get(code, TARIFFS["1"])
        labels = {"sbp": "СБП", "card": "Карта", "crypto": "CryptoBot", "stars": "Telegram Stars", "other": "Другие способы"}
        if callback.message:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"Оплатить — {labels.get(method, method)}", callback_data="vpn:demo_pay")],
                [InlineKeyboardButton(text="✅ Проверить оплату", callback_data="vpn:check")],
                _back(f"vpn:tariff:{code}"),
            ])
            await _screen(callback.message, f"📦 <b>Заказ: CEA-VPN-DEMO</b>\n\nVPN: <b>{name}</b>\nДоступно: <b>до 3 устройств</b>\nОплата: <b>{labels.get(method, method)}</b>\nСумма: <b>{old}₽</b>\n\n<blockquote>▶ Демонстрационный экран оплаты</blockquote>\n\nНа этом этапе деньги не списываются, VPN не подключается.", kb)
        await callback.answer()

    @router.callback_query(F.data.in_({"vpn:demo_pay", "vpn:check"}))
    async def demo_notice(callback: CallbackQuery) -> None:
        await callback.answer("Это визуальный прототип: оплата и VPN пока не подключены.", show_alert=True)

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
