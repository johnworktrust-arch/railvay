from __future__ import annotations

from typing import Any, Dict, Iterable
from urllib.parse import urlencode

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from ceai.formatting import format_coin_amount
from ceai.pricing import telegram_stars_amount_for_rub


PROFILE_BUTTON = "👤 Мой профиль"
GIFT_BUTTON = "🎁 Бесплатный доступ"
START_WORK_BUTTON = "Начать работу 🚀"
TEXT_AI_BUTTON = "💡Нейросети: ChatGPT, DeepSeek"
PHOTO_AI_BUTTON = "🖼 Фото с AI"
VIDEO_AI_BUTTON = "🎬 Видео с AI"
VOICE_AI_BUTTON = "🎙 Озвучка с AI"
HELP_BUTTON = "🆘 Помощь"
HISTORY_BUTTON = "🕘 История"
REFERRAL_BUTTON = "💰 Заработать"
BACK_TO_MENU_BUTTON = "⬅️ Назад"
MAIN_MENU_BUTTON = "🏠 Главное меню"
ADD_TEXT_CHAT_BUTTON = "➕ Добавить чат"
DELETE_CURRENT_TEXT_CHAT_BUTTON = "🗑 Удалить текущий чат"
BUY_CRYSTALS_BUTTON = "Купить коины отдельно"

REPLY_MENU_BUTTONS = {
    PROFILE_BUTTON,
    GIFT_BUTTON,
    START_WORK_BUTTON,
    TEXT_AI_BUTTON,
    PHOTO_AI_BUTTON,
    VIDEO_AI_BUTTON,
    VOICE_AI_BUTTON,
    HELP_BUTTON,
    HISTORY_BUTTON,
    REFERRAL_BUTTON,
    BACK_TO_MENU_BUTTON,
}


def main_menu_keyboard(
    *, gift_claimed: bool = False, support_username: str = "cea_help"
) -> InlineKeyboardMarkup:
    support_username = support_username.strip().lstrip("@") or "cea_help"
    rows = []
    if not gift_claimed:
        rows.append(
            [
                InlineKeyboardButton(
                    text=GIFT_BUTTON,
                    callback_data="menu:gift",
                    style="success",
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=START_WORK_BUTTON,
                    callback_data="menu:work",
                    style="primary",
                )
            ],
            [InlineKeyboardButton(text=PROFILE_BUTTON, callback_data="menu:home")],
            [InlineKeyboardButton(text=REFERRAL_BUTTON, callback_data="menu:referral")],
            [
                InlineKeyboardButton(
                    text="🆘 Поддержка",
                    url=f"https://t.me/{support_username}",
                ),
                InlineKeyboardButton(
                    text="🛡 О сервисе", callback_data="menu:about"
                ),
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def work_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
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
            ],
            [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:main")],
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
            [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:main")],
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


def work_access_required_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Подписка и тарифы", callback_data="menu:plans"
                )
            ],
            [
                InlineKeyboardButton(
                    text=GIFT_BUTTON, callback_data="menu:gift"
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


def about_service_keyboard(
    *, public_offer_url: str, support_username: str = "cea_help"
) -> InlineKeyboardMarkup:
    support_username = support_username.strip().lstrip("@") or "cea_help"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📄 Публичная оферта", url=public_offer_url
                ),
                InlineKeyboardButton(
                    text="🔒 Политика конфиденциальности", url=public_offer_url
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎟 Ввести промокод", callback_data="promo:placeholder"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🆘 Написать в поддержку",
                    url=f"https://t.me/{support_username}",
                )
            ],
            [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:main")],
        ]
    )


def gift_subscription_keyboard(
    *, info_channel_url: str = "https://t.me/ceafamily"
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📣 Подписаться на канал",
                    url=info_channel_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Проверить подписку",
                    callback_data="gift:check",
                )
            ],
            [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:main")],
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


def referral_keyboard(referral_link: str) -> InlineKeyboardMarkup:
    share_text = (
        "🤖 Попробуй Cea AI — здесь собраны современные нейросети для текста, "
        "фото, видео и озвучки!\n\n"
        "🎁 Забирай бесплатный доступ 👇"
    )
    share_url = "https://t.me/share/url?" + urlencode(
        {"url": referral_link, "text": share_text}
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💌 Пригласить", url=share_url)],
            [InlineKeyboardButton(text="💰 Вывести", callback_data="referral:withdraw")],
            [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:main")],
        ]
    )


def plans_keyboard(
    plans: Iterable[Dict[str, Any]], *, has_active_subscription: bool = False
) -> InlineKeyboardMarkup:
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
    if has_active_subscription:
        rows.append(
            [
                InlineKeyboardButton(
                    text="❌ Отменить подписку",
                    callback_data="subscription:cancel_placeholder",
                )
            ]
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
                    text="S - 139₽ - 10 коинов", callback_data="crystals:s"
                )
            ],
            [
                InlineKeyboardButton(
                    text="M - 499₽ - 40 коинов (-10%)",
                    callback_data="crystals:m",
                )
            ],
            [
                InlineKeyboardButton(
                    text="L - 999₽ - 85 коинов (-15%)",
                    callback_data="crystals:l",
                )
            ],
            [
                InlineKeyboardButton(
                    text="XL - 2990₽ - 270 коинов (-20%)",
                    callback_data="crystals:xl",
                )
            ],
            [
                InlineKeyboardButton(
                    text="XXL - 9000₽ - 850 коинов (-24%)",
                    callback_data="crystals:xxl",
                )
            ],
            [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:plans")],
        ]
    )


def _plan_choice_label(plan: Dict[str, Any]) -> str:
    price_rub = int(plan.get("price_rub") or 0)
    stars = telegram_stars_amount_for_rub(price_rub)
    return f"{plan['name']} — {price_rub} ₽ / {stars} ⭐"


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
    rows.append(
        [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:main")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tts_voice_keyboard() -> InlineKeyboardMarkup:
    voices = (
        ("Alloy", "alloy"),
        ("Echo", "echo"),
        ("Fable", "fable"),
        ("Onyx", "onyx"),
        ("Nova", "nova"),
        ("Shimmer", "shimmer"),
    )
    rows = [
        [
            InlineKeyboardButton(
                text=label,
                callback_data=f"tts:voice:{voice}",
            )
            for label, voice in voices[index : index + 2]
        ]
        for index in range(0, len(voices), 2)
    ]
    rows.append(
        [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:work")]
    )
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
            [
                InlineKeyboardButton(
                    text=BACK_TO_MENU_BUTTON, callback_data="menu:main"
                )
            ],
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


def history_keyboard(
    generations: Iterable[Dict[str, Any]], *, page: int, pages: int
) -> InlineKeyboardMarkup:
    number_buttons = [
        InlineKeyboardButton(
            text=f"#{generation['id']}",
            callback_data=f"history:view:{generation['id']}:{page}",
        )
        for generation in generations
    ]
    rows: list[list[InlineKeyboardButton]] = []
    if number_buttons:
        rows.append(number_buttons)

    pager: list[InlineKeyboardButton] = []
    if page > 1:
        pager.append(
            InlineKeyboardButton(
                text="⬅️ Пред страница", callback_data=f"history:page:{page - 1}"
            )
        )
    if page < pages:
        pager.append(
            InlineKeyboardButton(
                text="➡️ След страница", callback_data=f"history:page:{page + 1}"
            )
        )
    if pager:
        rows.append(pager)

    rows.append([InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def history_result_keyboard(*, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ К истории", callback_data=f"history:page:{page}"
                )
            ],
            [InlineKeyboardButton(text=BACK_TO_MENU_BUTTON, callback_data="menu:main")],
        ]
    )


def admin_menu_keyboard(*, maintenance_active: bool = False) -> InlineKeyboardMarkup:
    maintenance_text = (
        "🛠 Тех работы включены"
        if maintenance_active
        else "🛠 Тех работы выключены"
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
