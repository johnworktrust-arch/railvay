from __future__ import annotations

from typing import Any, Dict, Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="AI-инструменты", callback_data="menu:models")],
            [InlineKeyboardButton(text="Баланс", callback_data="menu:balance")],
            [InlineKeyboardButton(text="Тарифы", callback_data="menu:plans")],
            [InlineKeyboardButton(text="История", callback_data="menu:history")],
            [InlineKeyboardButton(text="Поддержка", callback_data="menu:support")],
        ]
    )


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Главное меню", callback_data="menu:home")]
        ]
    )


def plans_keyboard(plans: Iterable[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"Выбрать {plan['name']}", callback_data=f"buy:{plan['code']}"
            )
        ]
        for plan in plans
    ]
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_keyboard(payment_id: int, payment_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Оплатить тестово", callback_data=f"pay:{payment_id}"
                )
            ],
            [InlineKeyboardButton(text="Mock payment URL", url=payment_url)],
            [InlineKeyboardButton(text="Главное меню", callback_data="menu:home")],
        ]
    )


def models_keyboard(models: Iterable[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{model['display_name']} "
                    f"({model['generation_type']}, {model['coins_cost']} coins)"
                ),
                callback_data=f"model:{model['id']}",
            )
        ]
        for model in models
    ]
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
