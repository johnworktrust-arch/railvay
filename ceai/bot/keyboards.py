from __future__ import annotations

from typing import Any, Dict, Iterable

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


PROFILE_BUTTON = "👤 Профиль"
TEXT_AI_BUTTON = "🤖 Нейронки: GPT, DeepSeq"
PHOTO_AI_BUTTON = "🖼 Фото с AI"
VIDEO_AI_BUTTON = "🎬 Видео с AI"
VOICE_AI_BUTTON = "🎙 Озвучка с AI"
HELP_BUTTON = "🆘 Помощь"
HISTORY_BUTTON = "🕘 История"

REPLY_MENU_BUTTONS = {
    PROFILE_BUTTON,
    TEXT_AI_BUTTON,
    PHOTO_AI_BUTTON,
    VIDEO_AI_BUTTON,
    VOICE_AI_BUTTON,
    HELP_BUTTON,
    HISTORY_BUTTON,
}


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=PROFILE_BUTTON)],
            [KeyboardButton(text=TEXT_AI_BUTTON)],
            [KeyboardButton(text=PHOTO_AI_BUTTON), KeyboardButton(text=VIDEO_AI_BUTTON)],
            [KeyboardButton(text=VOICE_AI_BUTTON)],
            [KeyboardButton(text=HISTORY_BUTTON), KeyboardButton(text=HELP_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие",
    )


def back_to_menu_keyboard() -> ReplyKeyboardMarkup:
    return main_menu_keyboard()


def inline_back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Профиль", callback_data="menu:home")]
        ]
    )


def plans_keyboard(plans: Iterable[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"💳 Выбрать {plan['name']}",
                callback_data=f"buy:{plan['code']}",
            )
        ]
        for plan in plans
    ]
    rows.append([InlineKeyboardButton(text="👤 Профиль", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_keyboard(payment_id: int, payment_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Оплатить тестово", callback_data=f"pay:{payment_id}"
                )
            ],
            [InlineKeyboardButton(text="🔗 Тестовая ссылка оплаты", url=payment_url)],
            [InlineKeyboardButton(text="👤 Профиль", callback_data="menu:home")],
        ]
    )


def _model_emoji(generation_type: str) -> str:
    return {
        "text": "🤖",
        "image": "🖼",
        "video": "🎬",
        "tts": "🎙",
        "seo": "🔎",
    }.get(generation_type, "✨")


def models_keyboard(models: Iterable[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{_model_emoji(model['generation_type'])} {model['display_name']} "
                    f"({model['generation_type']}, {model['coins_cost']} coins)"
                ),
                callback_data=f"model:{model['id']}",
            )
        ]
        for model in models
    ]
    rows.append([InlineKeyboardButton(text="👤 Профиль", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
