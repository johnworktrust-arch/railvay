from __future__ import annotations

from contextlib import suppress
from typing import Any

from aiogram import Bot

from ceai.bot.keyboards import main_menu_button_keyboard
from ceai.formatting import format_coin_amount
from ceai.services.app import AppServices


def format_payment_notification(result: Any) -> str | None:
    if not result.processed or result.duplicate or not result.payment:
        return None
    if result.credited_coins > 0 and result.subscription:
        return (
            "✅ Оплата прошла успешно.\n\n"
            f"Начислено {format_coin_amount(result.credited_coins)}.\n"
            "Текущий баланс: "
            f"{format_coin_amount(result.subscription['coins_balance_cache'])}.\n\n"
            "Тариф активирован. Можно возвращаться в главное меню."
        )
    if result.message == "Payment canceled":
        return (
            "❌ Оплата не завершена.\n\n"
            "Коины не начислены. Если вы закрыли страницу оплаты случайно, "
            "выберите тариф и попробуйте ещё раз."
        )
    return None


async def notify_payment_result(
    *, bot: Bot, services: AppServices, result: Any
) -> None:
    text = format_payment_notification(result)
    if not text or not result.payment:
        return
    user = services.users.get_by_id(int(result.payment["user_id"]))
    if not user:
        return
    with suppress(Exception):
        await bot.send_message(
            chat_id=user["telegram_id"],
            text=text,
            reply_markup=main_menu_button_keyboard(),
        )
