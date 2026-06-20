from __future__ import annotations

from typing import Any, Dict, Iterable

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


PROFILE_BUTTON = "👤 Профиль"
TEXT_AI_BUTTON = "🤖 Нейронки: ChatGPT, DeepSeek"
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


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users:1")],
            [InlineKeyboardButton(text="🔎 Поиск", callback_data="admin:search")],
        ]
    )


def admin_users_keyboard(
    users: Iterable[Dict[str, Any]], *, page: int, pages: int
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"👤 {user_label(user)}", callback_data=f"admin:user:{user['id']}"
            )
        ]
        for user in users
    ]
    pager = []
    if page > 1:
        pager.append(
            InlineKeyboardButton(text="⬅️", callback_data=f"admin:users:{page - 1}")
        )
    if page < pages:
        pager.append(
            InlineKeyboardButton(text="➡️", callback_data=f"admin:users:{page + 1}")
        )
    if pager:
        rows.append(pager)
    rows.append([InlineKeyboardButton(text="⬅️ Админка", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_card_keyboard(
    user: Dict[str, Any], *, can_manage: bool
) -> InlineKeyboardMarkup:
    rows = []
    if can_manage:
        if user["is_blocked"]:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✅ Разбанить", callback_data=f"admin:unban:{user['id']}"
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🚫 Забанить", callback_data=f"admin:ban:{user['id']}"
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ Начислить coins",
                    callback_data=f"admin:credit:{user['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Пользователи", callback_data="admin:users:1")])
    rows.append([InlineKeyboardButton(text="⬅️ Админка", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Админка", callback_data="admin:home")]
        ]
    )


def user_label(user: Dict[str, Any]) -> str:
    username = user.get("username")
    if username:
        return f"@{username} · ID {user['id']}"
    name = " ".join(
        part for part in [user.get("first_name"), user.get("last_name")] if part
    ).strip()
    return f"{name or user['telegram_id']} · ID {user['id']}"
