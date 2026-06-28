from __future__ import annotations

from typing import Any, Dict, Iterable

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from ceai.formatting import format_coin_amount


PROFILE_BUTTON = "👤 Профиль"
TEXT_AI_BUTTON = "💡Нейросети: ChatGPT, DeepSeek"
PHOTO_AI_BUTTON = "🖼 Фото с AI"
VIDEO_AI_BUTTON = "🎬 Видео с AI"
VOICE_AI_BUTTON = "🎙 Озвучка с AI"
HELP_BUTTON = "🆘 Помощь"
HISTORY_BUTTON = "🕘 История"
BACK_TO_MENU_BUTTON = "⬅️ Назад"
MAIN_MENU_BUTTON = "🏠 Главное меню"
ADD_TEXT_CHAT_BUTTON = "➕ Добавить чат"
DELETE_CURRENT_TEXT_CHAT_BUTTON = "🗑 Удалить текущий чат"
BUY_CRYSTALS_BUTTON = "Купить коины отдельно"

REPLY_MENU_BUTTONS = {
    PROFILE_BUTTON,
    TEXT_AI_BUTTON,
    PHOTO_AI_BUTTON,
    VIDEO_AI_BUTTON,
    VOICE_AI_BUTTON,
    HELP_BUTTON,
    HISTORY_BUTTON,
    BACK_TO_MENU_BUTTON,
}


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=PROFILE_BUTTON, callback_data="menu:home")],
            [InlineKeyboardButton(text=TEXT_AI_BUTTON, callback_data="models:type:text")],
            [
                InlineKeyboardButton(
                    text=PHOTO_AI_BUTTON, callback_data="models:type:image"
                ),
                InlineKeyboardButton(
                    text=VIDEO_AI_BUTTON, callback_data="models:type:video"
                ),
            ],
            [InlineKeyboardButton(text=VOICE_AI_BUTTON, callback_data="models:type:tts")],
            [
                InlineKeyboardButton(text=HISTORY_BUTTON, callback_data="menu:history"),
                InlineKeyboardButton(text=HELP_BUTTON, callback_data="menu:support"),
            ],
        ]
    )


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Подписка и тарифы", callback_data="menu:plans"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🤝 Реферальная программа", callback_data="menu:referral"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🆘 Поддержка", callback_data="menu:support"
                )
            ],
            [
                InlineKeyboardButton(
                    text=BACK_TO_MENU_BUTTON, callback_data="menu:main"
                )
            ],
        ]
    )


def subscription_required_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Подписка и тарифы", callback_data="menu:plans"
                )
            ],
        ]
    )


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:main")]
        ]
    )


def main_menu_button_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=MAIN_MENU_BUTTON, callback_data="menu:main")]
        ]
    )


def inline_back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return back_to_menu_keyboard()


def referral_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💰 Вывести", callback_data="referral:withdraw"),
                InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:main"),
            ]
        ]
    )


def onboarding_continue_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Продолжить", callback_data="onboarding:continue")]
        ]
    )


def onboarding_links_keyboard(
    *, info_channel_url: str = "", support_username: str = "cea_help"
) -> InlineKeyboardMarkup:
    rows = []
    if info_channel_url:
        rows.append([InlineKeyboardButton(text="📢 Cea Family", url=info_channel_url)])
    username = support_username.strip().lstrip("@") or "cea_help"
    rows.append(
        [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{username}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plans_keyboard(plans: Iterable[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=_plan_choice_label(plan),
                callback_data=f"buy:{plan['code']}",
            )
        ]
        for plan in plans
    ]
    rows.append(
        [InlineKeyboardButton(text=BUY_CRYSTALS_BUTTON, callback_data="coins:buy")]
    )
    rows.append(
        [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:home")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_methods_keyboard(plan_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Карта / СБП",
                    callback_data=f"pay_method:{plan_code}:card_sbp",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐️ Telegram Stars",
                    callback_data=f"pay_method:{plan_code}:telegram_stars",
                )
            ],
            [
                InlineKeyboardButton(
                    text=BACK_TO_MENU_BUTTON, callback_data="menu:plans"
                )
            ],
        ]
    )


def crystal_packages_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="S - 139₽ - 30 коинов", callback_data="crystals:s"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔥M - 499₽ - 110 коинов  (-2%)",
                    callback_data="crystals:m",
                )
            ],
            [
                InlineKeyboardButton(
                    text="L - 999₽ - 260 коинов  (-17%)",
                    callback_data="crystals:l",
                )
            ],
            [
                InlineKeyboardButton(
                    text="XL - 2990₽ - 1198 коинов  (-45%)",
                    callback_data="crystals:xl",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚡XXL - 9000₽ - 4300 коинов  (-55%)",
                    callback_data="crystals:xxl",
                )
            ],
            [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:plans")],
        ]
    )


def _plan_choice_label(plan: Dict[str, Any]) -> str:
    icon = {
        "start": "⭐️",
        "basic": "🔥",
        "pro": "⚡️",
    }.get(str(plan.get("code")), "💳")
    return f"{icon} {plan['name']} - {plan['price_rub']}руб"


def payment_keyboard(
    payment_id: int, payment_url: str, *, provider: str = "mock"
) -> InlineKeyboardMarkup:
    rows = []
    if provider == "mock":
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Оплатить", callback_data=f"pay:{payment_id}"
                )
            ]
        )
        rows.append(
            [InlineKeyboardButton(text="🔗 Ссылка оплаты", url=payment_url)]
        )
    else:
        rows.append([InlineKeyboardButton(text="💳 Оплатить", url=payment_url)])
    rows.append(
        [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:home")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
                text=model_choice_label(model), callback_data=f"model:{model['id']}"
            )
        ]
        for model in models
    ]
    rows.append([InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def model_choice_label(model: Dict[str, Any]) -> str:
    if model["generation_type"] == "text":
        return str(model["display_name"])
    return (
        f"{_model_emoji(model['generation_type'])} {model['display_name']} · "
        f"{format_coin_amount(model['coins_cost'])}"
    )


def text_chat_label(chat: Dict[str, Any], *, current_chat_id: int | None) -> str:
    return str(chat["title"])


def text_chat_keyboard(
    chats: Iterable[Dict[str, Any]], *, current_chat_id: int | None
) -> InlineKeyboardMarkup:
    default_rows: list[list[InlineKeyboardButton]] = []
    custom_rows: list[list[InlineKeyboardButton]] = []
    default_buffer: list[InlineKeyboardButton] = []
    for chat in chats:
        button = InlineKeyboardButton(
            text=text_chat_label(chat, current_chat_id=current_chat_id),
            callback_data=f"text_chat:select:{chat['id']}",
        )
        if chat["is_default"]:
            if chat["title"] == "Основной":
                default_rows.append([button])
            else:
                default_buffer.append(button)
                if len(default_buffer) == 2:
                    default_rows.append(default_buffer)
                    default_buffer = []
        else:
            custom_rows.append([button])
    if default_buffer:
        default_rows.append(default_buffer)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            *default_rows,
            *custom_rows,
            [
                InlineKeyboardButton(
                    text=ADD_TEXT_CHAT_BUTTON, callback_data="text_chat:add"
                )
            ],
            [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:main")],
        ]
    )


def text_chat_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=DELETE_CURRENT_TEXT_CHAT_BUTTON,
                    callback_data="text_chat:delete",
                )
            ],
            [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="text_chat:back")],
        ]
    )


def admin_menu_keyboard(*, maintenance_active: bool = False) -> InlineKeyboardMarkup:
    maintenance_text = (
        "🛠 Тех работы активированы" if maintenance_active else "🛠 Тех работы"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users:1")],
            [InlineKeyboardButton(text="🔎 Поиск", callback_data="admin:search")],
            [InlineKeyboardButton(text=maintenance_text, callback_data="admin:maintenance")],
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
                    text="➕ Начислить коины",
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
