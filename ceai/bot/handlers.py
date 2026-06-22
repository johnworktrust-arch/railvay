from __future__ import annotations

from html import escape
from typing import Any, Dict

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from ceai.bot.keyboards import (
    ADD_TEXT_CHAT_BUTTON,
    BACK_TO_MENU_BUTTON,
    DELETE_CURRENT_TEXT_CHAT_BUTTON,
    HELP_BUTTON,
    HISTORY_BUTTON,
    PHOTO_AI_BUTTON,
    PROFILE_BUTTON,
    REPLY_MENU_BUTTONS,
    TEXT_AI_BUTTON,
    VIDEO_AI_BUTTON,
    VOICE_AI_BUTTON,
    admin_back_keyboard,
    admin_menu_keyboard,
    admin_user_card_keyboard,
    admin_users_keyboard,
    back_to_menu_keyboard,
    main_menu_keyboard,
    model_choice_label,
    models_keyboard,
    onboarding_continue_keyboard,
    onboarding_links_keyboard,
    payment_keyboard,
    plans_keyboard,
    profile_keyboard,
    text_chat_keyboard,
    text_chat_label,
    text_chat_prompt_keyboard,
)
from ceai.config import DEFAULT_PUBLIC_OFFER_URL
from ceai.formatting import format_datetime_minute
from ceai.json_utils import loads_dict
from ceai.runtime_diagnostics import record_error, record_message
from ceai.services.app import AppServices
from ceai.services.exceptions import (
    BusinessRuleError,
    GenerationProviderFailedError,
    InsufficientCoinsError,
    NoActiveSubscriptionError,
    NotFoundError,
)


LAST_BOT_MESSAGE_ID = "last_bot_message_id"
LAST_BOT_MESSAGE_IDS = "last_bot_message_ids"
LAST_REPLY_KEYBOARD_SIGNATURE = "last_reply_keyboard_signature"
START_TEXT_ALIASES = {"старт", "/старт", "start", "/start", "начать"}


def _is_start_text(text: str | None) -> bool:
    return (text or "").strip().casefold() in START_TEXT_ALIASES


def _is_user_message(message: Message) -> bool:
    from_user = getattr(message, "from_user", None)
    return bool(from_user and not from_user.is_bot)


def _user_kwargs(message_or_callback: Message | CallbackQuery) -> Dict[str, Any]:
    from_user = message_or_callback.from_user
    return {
        "telegram_id": from_user.id,
        "username": from_user.username,
        "first_name": from_user.first_name,
        "last_name": from_user.last_name,
        "language_code": from_user.language_code,
    }


def _session_state_payload(
    services: AppServices, user_id: int
) -> tuple[str, Dict[str, Any]]:
    session = services.users.get_session(user_id)
    if not session:
        return "idle", {}
    return session["state"], loads_dict(session.get("payload"))


def _tracked_message_ids(payload: Dict[str, Any]) -> list[int]:
    ids: list[int] = []
    legacy_id = payload.get(LAST_BOT_MESSAGE_ID)
    if isinstance(legacy_id, int):
        ids.append(legacy_id)
    stored_ids = payload.get(LAST_BOT_MESSAGE_IDS)
    if isinstance(stored_ids, list):
        for message_id in stored_ids:
            if isinstance(message_id, int) and message_id not in ids:
                ids.append(message_id)
    return ids


def _reply_keyboard_signature(reply_markup: Any | None) -> list[list[str]] | str | None:
    if isinstance(reply_markup, ReplyKeyboardMarkup):
        return [[button.text for button in row] for row in reply_markup.keyboard]
    if isinstance(reply_markup, ReplyKeyboardRemove):
        return "remove"
    return None


def _set_dialog_state(
    services: AppServices,
    user_id: int,
    *,
    state: str,
    payload: Dict[str, Any] | None = None,
) -> None:
    _, current_payload = _session_state_payload(services, user_id)
    next_payload = dict(payload or {})
    if LAST_BOT_MESSAGE_ID in next_payload and LAST_BOT_MESSAGE_IDS not in next_payload:
        legacy_id = next_payload.pop(LAST_BOT_MESSAGE_ID)
        if isinstance(legacy_id, int):
            next_payload[LAST_BOT_MESSAGE_IDS] = [legacy_id]
    if LAST_BOT_MESSAGE_IDS not in next_payload:
        current_ids = _tracked_message_ids(current_payload)
        if current_ids:
            next_payload[LAST_BOT_MESSAGE_IDS] = current_ids
    if (
        LAST_REPLY_KEYBOARD_SIGNATURE in current_payload
        and LAST_REPLY_KEYBOARD_SIGNATURE not in next_payload
    ):
        next_payload[LAST_REPLY_KEYBOARD_SIGNATURE] = current_payload[
            LAST_REPLY_KEYBOARD_SIGNATURE
        ]
    services.users.set_session(user_id, state=state, payload=next_payload)


def _clear_dialog_state(services: AppServices, user_id: int) -> None:
    _set_dialog_state(services, user_id, state="idle", payload={})


def _reset_dialog_state(services: AppServices, user_id: int) -> None:
    services.users.set_session(user_id, state="idle", payload={})


def _remember_screen_message(
    services: AppServices,
    user_id: int,
    *,
    state: str,
    payload: Dict[str, Any],
    message_id: int,
    reply_markup: Any | None,
) -> None:
    payload.pop(LAST_BOT_MESSAGE_ID, None)
    payload[LAST_BOT_MESSAGE_IDS] = [message_id]
    reply_signature = _reply_keyboard_signature(reply_markup)
    if reply_signature is not None:
        payload[LAST_REPLY_KEYBOARD_SIGNATURE] = reply_signature
    services.users.set_session(user_id, state=state, payload=payload)


async def _edit_screen_message(
    message: Message,
    *,
    message_id: int,
    text: str,
    reply_markup: Any | None,
    parse_mode: str | None = None,
) -> Message | None:
    try:
        edited = await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=(
                reply_markup if isinstance(reply_markup, InlineKeyboardMarkup) else None
            ),
        )
        return edited if isinstance(edited, Message) else None
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).casefold():
            return message
        return None
    except TelegramForbiddenError:
        return None


async def _delete_screen_messages(message: Message, message_ids: list[int]) -> None:
    for message_id in message_ids:
        try:
            await message.bot.delete_message(
                chat_id=message.chat.id,
                message_id=message_id,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            pass


async def _delete_user_message(message: Message) -> None:
    if not _is_user_message(message):
        return
    try:
        await message.bot.delete_message(
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


async def _send_screen_message(
    message: Message,
    *,
    text: str,
    reply_markup: Any | None,
    parse_mode: str | None = None,
) -> Message:
    if isinstance(reply_markup, InlineKeyboardMarkup) and _is_user_message(message):
        sent = await message.bot.send_message(
            chat_id=message.chat.id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=ReplyKeyboardRemove(),
        )
        try:
            edited = await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=sent.message_id,
                reply_markup=reply_markup,
            )
            return edited if isinstance(edited, Message) else sent
        except (TelegramBadRequest, TelegramForbiddenError):
            try:
                await message.bot.delete_message(
                    chat_id=message.chat.id,
                    message_id=sent.message_id,
                )
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
            return await message.bot.send_message(
                chat_id=message.chat.id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )

    return await message.bot.send_message(
        chat_id=message.chat.id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )


async def _show_screen(
    message: Message,
    services: AppServices,
    user_id: int,
    text: str,
    *,
    reply_markup: Any | None = None,
    delete_current: bool = False,
    parse_mode: str | None = None,
) -> Message:
    state, payload = _session_state_payload(services, user_id)
    tracked_ids = _tracked_message_ids(payload)
    last_message_id = tracked_ids[-1] if tracked_ids else None
    replace_current = isinstance(
        reply_markup, (ReplyKeyboardMarkup, ReplyKeyboardRemove)
    ) or (delete_current and _is_user_message(message))

    # Bottom-keyboard actions arrive as user messages, so they should replace
    # the previous bot screen. Inline callback actions keep editing the message.
    if replace_current:
        if tracked_ids:
            await _delete_screen_messages(message, tracked_ids)
        sent = await _send_screen_message(
            message,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        _remember_screen_message(
            services,
            user_id,
            state=state,
            payload=payload,
            message_id=sent.message_id,
            reply_markup=reply_markup,
        )
        return sent

    if last_message_id is not None:
        edited = await _edit_screen_message(
            message,
            message_id=last_message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        if edited is not None:
            _remember_screen_message(
                services,
                user_id,
                state=state,
                payload=payload,
                message_id=last_message_id,
                reply_markup=reply_markup,
            )
            return edited

    # Inline keyboards are part of the message and can be edited with the text.
    # If editing fails, we send a fresh message as the fallback screen.
    sent = await _send_screen_message(
        message,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
    _remember_screen_message(
        services,
        user_id,
        state=state,
        payload=payload,
        message_id=sent.message_id,
        reply_markup=reply_markup,
    )
    return sent


async def _show_onboarding_followup(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    delete_current: bool = False,
) -> None:
    if message.message_id:
        try:
            await message.bot.delete_message(
                chat_id=message.chat.id,
                message_id=message.message_id,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            try:
                await message.bot.edit_message_reply_markup(
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=None,
                )
            except (TelegramBadRequest, TelegramForbiddenError):
                pass

    hint = await message.bot.send_message(
        chat_id=message.chat.id,
        text=_format_onboarding_hint(),
    )
    promo = await message.bot.send_message(
        chat_id=message.chat.id,
        text=_format_onboarding_promo(),
        reply_markup=onboarding_links_keyboard(
            info_channel_url=services.settings.info_channel_url,
            support_username=services.settings.support_username,
        ),
    )
    menu = await message.bot.send_message(
        chat_id=message.chat.id,
        text=_format_main_menu(),
        reply_markup=main_menu_keyboard(),
    )
    services.users.set_session(
        user_id,
        state="idle",
        payload={
            LAST_BOT_MESSAGE_IDS: [menu.message_id],
            LAST_REPLY_KEYBOARD_SIGNATURE: _reply_keyboard_signature(
                main_menu_keyboard()
            ),
        },
    )


def _profile_link(user: Dict[str, Any]) -> str:
    username = str(user.get("username") or "").strip()
    if username:
        label = f"@{username}"
    else:
        label = " ".join(
            part
            for part in [user.get("first_name"), user.get("last_name")]
            if part
        ).strip()
    if not label:
        label = f"ID {user.get('telegram_id') or user.get('id')}"
    telegram_id = int(user.get("telegram_id") or 0)
    if telegram_id <= 0:
        return escape(label)
    return f'<a href="tg://user?id={telegram_id}">{escape(label)}</a>'


def _format_menu(
    user: Dict[str, Any],
    subscription: Dict[str, Any] | None,
    *,
    invited_users_count: int = 0,
) -> str:
    if subscription:
        balance = subscription["coins_balance_cache"]
        plan = subscription["plan_name"]
        ends_at = subscription["ends_at"][:10]
        sub_line = f"Подписка: {plan} до {ends_at}"
    else:
        balance = 0
        sub_line = "Подписка: нет активной"
    return (
        f"👤 {_profile_link(user)}\n\n"
        f"Баланс: {balance} coins\n"
        f"{sub_line}\n"
        f"Приглашенные пользователи: {invited_users_count}"
    )


def _format_onboarding_greeting(public_offer_url: str) -> str:
    offer_url = public_offer_url.strip() or DEFAULT_PUBLIC_OFFER_URL
    return (
        "👋 Приветствую в Cea AI!\n\n"
        "Продолжая, вы соглашаетесь с условиями использования сервиса "
        f"(Документ оферты здесь: {offer_url})."
    )


def _format_onboarding_hint() -> str:
    return (
        "ℹ️ Чтобы узнать больше о своём аккаунте и тарифах, нажмите кнопку "
        "«Профиль»."
    )


def _format_onboarding_promo() -> str:
    return (
        "☝️ В двух словах об основных инструментах чат-бота.\n\n"
        "Cea AI предоставляет доступ к актуальным AI-инструментам в одном "
        "Telegram-боте: текстовые нейросети, фото с AI, видео с AI и "
        "озвучка текста.\n\n"
        "👇 Следите за обновлениями в канале или напишите в поддержку."
    )


def _format_main_menu() -> str:
    return "🏠 Главное меню\nВыберите нужный раздел 👇"


def _format_plans(plans: list[Dict[str, Any]]) -> str:
    lines = ["Тарифы CeaAI:"]
    for plan in plans:
        lines.append(
            f"{plan['name']}: {plan['price_rub']} руб. / "
            f"{plan['coins_amount']} coins / {plan['duration_days']} дней"
        )
    return "\n".join(lines)


def _format_models(models: list[Dict[str, Any]]) -> str:
    lines = []
    for model in models:
        config = loads_dict(model.get("config"))
        description = str(config.get("ui_description") or "").strip()
        lines.extend(
            [
                f"🤖 {model['display_name']}",
                f"Стоимость: {model['coins_cost']} coins за запрос.",
            ]
        )
        if description:
            lines.append(description)
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _model_choice_payload(models: list[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "model_choices": {
            model_choice_label(model): int(model["id"]) for model in models
        }
    }


def _text_chat_payload(
    model: Dict[str, Any],
    chats: list[Dict[str, Any]],
    current_chat: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        "model_price_id": int(model["id"]),
        "current_text_chat_id": int(current_chat["id"]) if current_chat else 0,
        "text_chat_choices": {
            text_chat_label(chat, current_chat_id=None): int(chat["id"])
            for chat in chats
        },
    }


def _format_text_chat_list_screen(
    model: Dict[str, Any], *, notice: str | None = None
) -> str:
    lines = []
    if notice:
        lines.extend([notice, ""])
    lines.extend(
        [
            f"🤖 {model['display_name']}",
            "",
            f"Стоимость 1 запроса: {model['coins_cost']} coins",
            "",
            "Выберите чат ниже:",
        ]
    )
    return "\n".join(lines)


def _format_text_chat_prompt_screen(
    model: Dict[str, Any], current_chat: Dict[str, Any], *, notice: str | None = None
) -> str:
    lines = []
    if notice:
        lines.extend([notice, ""])
    lines.extend(
        [
            f"🤖 {model['display_name']}",
            f"Чат: {current_chat['title']}",
            "",
            f"Стоимость 1 запроса: {model['coins_cost']} coins",
            "Напишите вопрос сообщением ниже.",
        ]
    )
    return "\n".join(lines)


def _format_generation_result(result: Dict[str, Any]) -> str:
    kind = result.get("kind")
    if kind == "text":
        body = str(result.get("text", ""))
    elif kind in {"image", "video"}:
        body = f"{result.get('caption', 'Mock result')}\n{result.get('url')}"
    elif kind == "tts":
        body = f"{result.get('message', 'Mock TTS result')}\n{result.get('url')}"
    else:
        body = str(result)
    return body


def _format_history(rows: list[Dict[str, Any]]) -> str:
    if not rows:
        return "История пока пустая."
    lines = ["Последние генерации:"]
    for row in rows:
        prompt = row.get("prompt_payload", {}).get("text", "")
        if len(prompt) > 60:
            prompt = prompt[:57] + "..."
        lines.append(
            f"#{row['id']} {row['model_display_name']} — {row['status']} — "
            f"{row['coins_charged']} coins — {prompt}"
        )
    return "\n".join(lines)


def _format_admin_stats(stats: Dict[str, Any]) -> str:
    return (
        "🛠 Админка CeaAI\n\n"
        "📊 Статистика\n"
        f"Пользователей: {stats['users_total']}\n"
        f"Активных подписок: {stats['active_subscriptions']}\n"
        f"Paid-платежей: {stats['paid_payments']}\n"
        f"Mock-выручка: {stats['mock_revenue_rub']} руб.\n"
        f"Генераций: {stats['generations_total']}\n"
        f"Баланс активных подписок: {stats['active_balance_total']} coins"
    )


def _telegram_profile(user: Dict[str, Any]) -> str:
    username = user.get("username")
    if username:
        return f"@{username}"
    return f"tg://user?id={user['telegram_id']}"


def _format_admin_users(
    users: list[Dict[str, Any]], *, page: int, pages: int, total: int
) -> str:
    lines = [f"👥 Пользователи ({total})", f"Страница {page}/{pages}", ""]
    if not users:
        lines.append("Пользователей пока нет.")
    for user in users:
        blocked = "🚫 " if user["is_blocked"] else ""
        balance = user.get("coins_balance_cache")
        plan = user.get("plan_name") or "без тарифа"
        balance_text = f"{balance} coins" if balance is not None else "0 coins"
        lines.append(
            f"{blocked}#{user['id']} {_telegram_profile(user)} · {plan} · {balance_text}"
        )
    return "\n".join(lines)


def _format_admin_user_card(card: Dict[str, Any]) -> str:
    subscription = card.get("subscription")
    payments = card.get("payments") or {}
    generations = card.get("generations") or {}
    name = " ".join(
        part for part in [card.get("first_name"), card.get("last_name")] if part
    ).strip()
    if subscription:
        tariff = (
            f"{subscription['plan_name']} · {subscription['status']} · "
            f"{subscription['coins_balance_cache']} coins"
        )
    else:
        tariff = "нет активной подписки"
    return (
        f"👤 Пользователь #{card['id']}\n\n"
        f"Имя: {name or '—'}\n"
        f"Профиль: {_telegram_profile(card)}\n"
        f"Telegram ID: {card['telegram_id']}\n"
        f"Дата регистрации: {format_datetime_minute(card['created_at'])}\n"
        f"Последний визит: {format_datetime_minute(card['last_seen_at'])}\n"
        f"Статус: {'заблокирован' if card['is_blocked'] else 'активен'}\n"
        f"Тариф: {tariff}\n"
        f"Платежи: {payments.get('paid_count', 0)} paid / "
        f"{payments.get('paid_amount_rub', 0)} руб.\n"
        f"Генерации: {generations.get('total', 0)}\n"
        f"Потрачено: {generations.get('spent_coins', 0)} coins"
    )


async def _send_main_menu(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    intro: str | None = None,
    delete_current: bool = False,
) -> None:
    subscription = services.subscriptions.active_for_user(user_id)
    profile_user = services.users.get_by_id(user_id) or {"id": user_id}
    invited_users_count = services.users.count_invited_users(user_id)
    text = _format_menu(
        profile_user,
        subscription,
        invited_users_count=invited_users_count,
    )
    if intro:
        text = f"{intro}\n\n{text}"
    await _show_screen(
        message,
        services,
        user_id,
        text,
        reply_markup=profile_keyboard(),
        delete_current=delete_current,
        parse_mode="HTML",
    )


async def _send_onboarding_greeting(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    delete_current: bool = False,
) -> None:
    _set_dialog_state(services, user_id, state="onboarding_waiting_continue")
    await _show_screen(
        message,
        services,
        user_id,
        _format_onboarding_greeting(services.settings.public_offer_url),
        reply_markup=onboarding_continue_keyboard(),
        delete_current=delete_current,
    )


async def _send_admin_home(
    message: Message, services: AppServices, user_id: int, *, delete_current: bool = False
) -> None:
    await _show_screen(
        message,
        services,
        user_id,
        "🛠 Админка CeaAI\nВыберите раздел.",
        reply_markup=admin_menu_keyboard(),
        delete_current=delete_current,
    )


async def _send_menu_screen(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    delete_current: bool = False,
) -> None:
    await _show_screen(
        message,
        services,
        user_id,
        _format_main_menu(),
        reply_markup=main_menu_keyboard(),
        delete_current=delete_current,
    )


async def _send_blocked_notice(
    message: Message, services: AppServices, user_id: int, *, delete_current: bool = True
) -> None:
    await _show_screen(
        message,
        services,
        user_id,
        "Ваш аккаунт заблокирован. Обратитесь в поддержку.",
        reply_markup=main_menu_keyboard(),
        delete_current=delete_current,
    )


def _is_blocked_regular_user(services: AppServices, user: Dict[str, Any]) -> bool:
    return services.admin.is_blocked_regular_user(user)


def _record_message(handler: str, message: Message) -> None:
    record_message(handler=handler, message=message)


async def _send_balance(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    delete_current: bool = False,
) -> None:
    subscription = services.subscriptions.active_for_user(user_id)
    if subscription:
        text = (
            f"Баланс: {subscription['coins_balance_cache']} coins\n"
            f"Подписка: {subscription['plan_name']} до {subscription['ends_at'][:10]}"
        )
    else:
        text = "Активной подписки нет. Выберите тариф и оплатите тестово."
    await _show_screen(
        message,
        services,
        user_id,
        text,
        reply_markup=main_menu_keyboard(),
        delete_current=delete_current,
    )


async def _send_plans(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    delete_current: bool = False,
) -> None:
    _set_dialog_state(services, user_id, state="waiting_payment_choice")
    plans = services.catalog.list_plans()
    await _show_screen(
        message,
        services,
        user_id,
        _format_plans(plans),
        reply_markup=plans_keyboard(plans),
        delete_current=delete_current,
    )


async def _send_history(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    delete_current: bool = False,
) -> None:
    rows = services.generations.list_recent(user_id=user_id, limit=10)
    await _show_screen(
        message,
        services,
        user_id,
        _format_history(rows),
        reply_markup=main_menu_keyboard(),
        delete_current=delete_current,
    )


async def _send_support(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    delete_current: bool = False,
) -> None:
    _clear_dialog_state(services, user_id)
    support_username = services.settings.support_username or "cea_help"
    await _show_screen(
        message,
        services,
        user_id,
        f"Поддержка: @{support_username}\n"
        "Напишите нам, если нужна помощь с аккаунтом, тарифом или генерацией.",
        reply_markup=main_menu_keyboard(),
        delete_current=delete_current,
    )


async def _send_models_for_types(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    generation_types: set[str],
    title: str,
    delete_current: bool = False,
) -> None:
    models = [
        model
        for model in services.catalog.list_models()
        if model["generation_type"] in generation_types
    ]
    if not models:
        await _show_screen(
            message,
            services,
            user_id,
            "Для этого раздела пока нет активных моделей.",
            reply_markup=main_menu_keyboard(),
            delete_current=delete_current,
        )
        return
    _set_dialog_state(
        services,
        user_id,
        state="waiting_model_choice",
        payload=_model_choice_payload(models),
    )
    await _show_screen(
        message,
        services,
        user_id,
        f"{title}\n\n{_format_models(models)}",
        reply_markup=models_keyboard(models),
        delete_current=delete_current,
    )


async def _send_text_chat_screen(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    model: Dict[str, Any],
    current_chat_id: int | None = None,
    notice: str | None = None,
    delete_current: bool = False,
) -> None:
    chats = services.text_chats.list_for_model(
        user_id=user_id, model_price_id=int(model["id"])
    )
    current_chat = None
    if current_chat_id is not None:
        for chat in chats:
            if int(chat["id"]) == current_chat_id:
                current_chat = chat
                break

    _set_dialog_state(
        services,
        user_id,
        state="waiting_text_chat_choice",
        payload=_text_chat_payload(model, chats, current_chat),
    )
    await _show_screen(
        message,
        services,
        user_id,
        _format_text_chat_list_screen(model, notice=notice),
        reply_markup=text_chat_keyboard(
            chats,
            current_chat_id=int(current_chat["id"]) if current_chat else None,
        ),
        delete_current=delete_current,
    )


async def _send_text_chat_prompt_screen(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    model: Dict[str, Any],
    current_chat: Dict[str, Any],
    notice: str | None = None,
    delete_current: bool = False,
) -> None:
    chats = services.text_chats.list_for_model(
        user_id=user_id, model_price_id=int(model["id"])
    )
    _set_dialog_state(
        services,
        user_id,
        state="waiting_text_chat_prompt",
        payload=_text_chat_payload(model, chats, current_chat),
    )
    await _show_screen(
        message,
        services,
        user_id,
        _format_text_chat_prompt_screen(model, current_chat, notice=notice),
        reply_markup=text_chat_prompt_keyboard(),
        delete_current=delete_current,
    )


async def _handle_reply_menu(
    message: Message, services: AppServices, user: Dict[str, Any]
) -> bool:
    text = (message.text or "").strip()
    text_lower = text.casefold()
    session_state, session_payload = _session_state_payload(services, user["id"])

    if session_state == "waiting_text_chat_prompt" and (
        text == BACK_TO_MENU_BUTTON or text_lower in {"назад", "back"}
    ):
        model_price_id = int(session_payload.get("model_price_id", 0))
        current_chat_id = int(session_payload.get("current_text_chat_id", 0))
        model = services.catalog.get_model(model_price_id)
        if model is None:
            _clear_dialog_state(services, user["id"])
            await _show_screen(
                message,
                services,
                user["id"],
                "Модель не найдена. Выберите нейросетку заново.",
                reply_markup=main_menu_keyboard(),
                delete_current=True,
            )
            return True
        await _send_text_chat_screen(
            message,
            services,
            user["id"],
            model=model,
            current_chat_id=current_chat_id if current_chat_id > 0 else None,
            delete_current=True,
        )
        return True

    if session_state == "waiting_text_chat_prompt" and text == DELETE_CURRENT_TEXT_CHAT_BUTTON:
        model_price_id = int(session_payload.get("model_price_id", 0))
        current_chat_id = int(session_payload.get("current_text_chat_id", 0))
        model = services.catalog.get_model(model_price_id)
        if model is None:
            _clear_dialog_state(services, user["id"])
            await _show_screen(
                message,
                services,
                user["id"],
                "Модель не найдена. Выберите нейросетку заново.",
                reply_markup=main_menu_keyboard(),
                delete_current=True,
            )
            return True
        try:
            services.text_chats.delete(user_id=user["id"], chat_id=current_chat_id)
            await _send_text_chat_screen(
                message,
                services,
                user["id"],
                model=model,
                notice="Чат удалён.",
                delete_current=True,
            )
        except NotFoundError as exc:
            await _send_text_chat_screen(
                message,
                services,
                user["id"],
                model=model,
                notice=str(exc),
                delete_current=True,
            )
        except BusinessRuleError as exc:
            current_chat = services.text_chats.get_active(
                user_id=user["id"], chat_id=current_chat_id
            )
            await _send_text_chat_prompt_screen(
                message,
                services,
                user["id"],
                model=model,
                current_chat=current_chat,
                notice=str(exc),
                delete_current=True,
            )
        return True

    if text_lower in {"menu", "/menu", "главное меню", "назад", "назад в меню"} or (
        text == BACK_TO_MENU_BUTTON
    ):
        _clear_dialog_state(services, user["id"])
        await _send_menu_screen(message, services, user["id"], delete_current=True)
        return True

    if session_state == "waiting_text_chat_name":
        model_price_id = int(session_payload.get("model_price_id", 0))
        model = services.catalog.get_model(model_price_id)
        if model is None:
            _clear_dialog_state(services, user["id"])
            await _show_screen(
                message,
                services,
                user["id"],
                "Модель не найдена. Выберите нейросетку заново.",
                reply_markup=main_menu_keyboard(),
                delete_current=True,
            )
            return True
        try:
            chat = services.text_chats.create_custom(
                user_id=user["id"], model_price_id=model_price_id, title=text
            )
            await _send_text_chat_prompt_screen(
                message,
                services,
                user["id"],
                model=model,
                current_chat=chat,
                notice=f"Чат «{chat['title']}» создан.",
                delete_current=True,
            )
        except BusinessRuleError as exc:
            await _show_screen(
                message,
                services,
                user["id"],
                str(exc),
                reply_markup=ReplyKeyboardRemove(),
                delete_current=True,
            )
        return True

    if session_state == "waiting_text_chat_choice":
        model_price_id = int(session_payload.get("model_price_id", 0))
        current_chat_id = int(session_payload.get("current_text_chat_id", 0))
        model = services.catalog.get_model(model_price_id)
        if model is None:
            _clear_dialog_state(services, user["id"])
            await _show_screen(
                message,
                services,
                user["id"],
                "Модель не найдена. Выберите нейросетку заново.",
                reply_markup=main_menu_keyboard(),
                delete_current=True,
            )
            return True

        if text == ADD_TEXT_CHAT_BUTTON:
            _set_dialog_state(
                services,
                user["id"],
                state="waiting_text_chat_name",
                payload={
                    "model_price_id": model_price_id,
                    "current_text_chat_id": current_chat_id,
                },
            )
            await _show_screen(
                message,
                services,
                user["id"],
                "Введите название нового чата.",
                reply_markup=ReplyKeyboardRemove(),
                delete_current=True,
            )
            return True

        choices = session_payload.get("text_chat_choices")
        if isinstance(choices, dict) and text in choices:
            chat_id = int(choices[text])
            chat = services.text_chats.get_active(user_id=user["id"], chat_id=chat_id)
            await _send_text_chat_prompt_screen(
                message,
                services,
                user["id"],
                model=model,
                current_chat=chat,
                notice=f"Чат «{chat['title']}» выбран.",
                delete_current=True,
            )
            return True

    if session_state == "waiting_model_choice":
        choices = session_payload.get("model_choices")
        if isinstance(choices, dict) and text in choices:
            model_price_id = int(choices[text])
            model = services.catalog.get_model(model_price_id)
            if model is None:
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    "Модель не найдена. Выберите нейросетку заново.",
                    reply_markup=main_menu_keyboard(),
                    delete_current=True,
                )
                return True
            if model["generation_type"] == "text":
                await _send_text_chat_screen(
                    message,
                    services,
                    user["id"],
                    model=model,
                    delete_current=True,
                )
                return True
            _set_dialog_state(
                services,
                user["id"],
                state="waiting_prompt",
                payload={"model_price_id": model_price_id},
            )
            await _show_screen(
                message,
                services,
                user["id"],
                "Отправьте prompt для выбранной модели.",
                reply_markup=back_to_menu_keyboard(),
                delete_current=True,
            )
            return True

    if text_lower in {"профиль", "/профиль", "profile", "/profile"} or text == PROFILE_BUTTON:
        _clear_dialog_state(services, user["id"])
        await _send_main_menu(message, services, user["id"], delete_current=True)
        return True

    if text_lower == "баланс":
        _clear_dialog_state(services, user["id"])
        await _send_main_menu(message, services, user["id"], delete_current=True)
        return True

    if text_lower == "тарифы":
        await _send_plans(message, services, user["id"], delete_current=True)
        return True

    if text_lower == "история" or text == HISTORY_BUTTON:
        _clear_dialog_state(services, user["id"])
        await _send_history(message, services, user["id"], delete_current=True)
        return True

    if text_lower in {"помощь", "help"} or text == HELP_BUTTON:
        await _send_support(message, services, user["id"], delete_current=True)
        return True

    if text_lower in {
        "chatgpt deepseek",
        "gpt deepseek",
        "gpt deepseak",
        "gpt deepseq",
        "нейронки: chatgpt, deepseek",
        "нейронки: gpt, deepseek",
        "нейронки: gpt, deepseq",
        "нейронки chatgpt deepseek",
        "нейронки gpt deepseek",
        "нейронки gpt deepseq",
    } or text == TEXT_AI_BUTTON:
        await _send_models_for_types(
            message,
            services,
            user["id"],
            generation_types={"text"},
            title="Выберите текстовую модель.",
            delete_current=True,
        )
        return True

    if text_lower == "фото с ai" or text == PHOTO_AI_BUTTON:
        await _send_models_for_types(
            message,
            services,
            user["id"],
            generation_types={"image"},
            title="Выберите модель для фото с AI.",
            delete_current=True,
        )
        return True

    if text_lower == "видео с ai" or text == VIDEO_AI_BUTTON:
        await _send_models_for_types(
            message,
            services,
            user["id"],
            generation_types={"video"},
            title="Выберите модель для видео с AI.",
            delete_current=True,
        )
        return True

    if text_lower in {"озвучка с ai", "озвучка текста"} or text == VOICE_AI_BUTTON:
        await _send_models_for_types(
            message,
            services,
            user["id"],
            generation_types={"tts"},
            title="Выберите модель для озвучки текста.",
            delete_current=True,
        )
        return True

    if text in REPLY_MENU_BUTTONS:
        await _send_main_menu(message, services, user["id"], delete_current=True)
        return True

    return False


def create_router(services: AppServices) -> Router:
    router = Router()

    @router.errors()
    async def bot_error(event: ErrorEvent) -> None:
        record_error(exception=event.exception, update=event.update)

    @router.message(Command("admin"))
    async def admin_command(message: Message) -> None:
        _record_message("admin_command", message)
        await _delete_user_message(message)
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        admin = services.admin.ensure_admin_access(user)
        if not admin:
            return
        _clear_dialog_state(services, user["id"])
        await _send_admin_home(message, services, user["id"], delete_current=True)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        _record_message("start", message)
        await _delete_user_message(message)
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        if _is_blocked_regular_user(services, user):
            await _send_blocked_notice(message, services, user["id"])
            return
        _reset_dialog_state(services, user["id"])
        await _send_onboarding_greeting(
            message, services, user["id"], delete_current=True
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        _record_message("help_command", message)
        await _delete_user_message(message)
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        if _is_blocked_regular_user(services, user):
            await _send_blocked_notice(message, services, user["id"])
            return
        await _send_support(message, services, user["id"], delete_current=True)

    @router.message(Command("menu"))
    async def menu_command(message: Message) -> None:
        _record_message("menu_command", message)
        await _delete_user_message(message)
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        if _is_blocked_regular_user(services, user):
            await _send_blocked_notice(message, services, user["id"])
            return
        _clear_dialog_state(services, user["id"])
        await _send_menu_screen(message, services, user["id"], delete_current=True)

    @router.message(Command("profile"))
    async def profile_command(message: Message) -> None:
        _record_message("profile_command", message)
        await _delete_user_message(message)
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        if _is_blocked_regular_user(services, user):
            await _send_blocked_notice(message, services, user["id"])
            return
        _clear_dialog_state(services, user["id"])
        await _send_main_menu(message, services, user["id"], delete_current=True)

    @router.callback_query(F.data == "onboarding:continue")
    async def onboarding_continue(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        _clear_dialog_state(services, user["id"])
        if callback.message:
            await _show_onboarding_followup(
                callback.message, services, user["id"], delete_current=True
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("admin:"))
    async def admin_callback(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        admin = services.admin.ensure_admin_access(user)
        if not admin:
            await callback.answer()
            return
        if not callback.message or not callback.data:
            await callback.answer()
            return

        parts = callback.data.split(":")
        action = parts[1] if len(parts) > 1 else "home"
        try:
            if action == "home":
                _clear_dialog_state(services, user["id"])
                await _send_admin_home(
                    callback.message, services, user["id"], delete_current=True
                )
            elif action == "stats":
                stats = services.admin.stats()
                await _show_screen(
                    callback.message,
                    services,
                    user["id"],
                    _format_admin_stats(stats),
                    reply_markup=admin_back_keyboard(),
                    delete_current=True,
                )
            elif action == "users":
                page = int(parts[2]) if len(parts) > 2 else 1
                data = services.admin.list_users(page=page)
                await _show_screen(
                    callback.message,
                    services,
                    user["id"],
                    _format_admin_users(
                        data["users"],
                        page=data["page"],
                        pages=data["pages"],
                        total=data["total"],
                    ),
                    reply_markup=admin_users_keyboard(
                        data["users"], page=data["page"], pages=data["pages"]
                    ),
                    delete_current=True,
                )
            elif action == "user":
                target_user_id = int(parts[2])
                card = services.admin.user_card(target_user_id)
                await _show_screen(
                    callback.message,
                    services,
                    user["id"],
                    _format_admin_user_card(card),
                    reply_markup=admin_user_card_keyboard(
                        card, can_manage=services.admin.can_manage(admin)
                    ),
                    delete_current=True,
                )
            elif action in {"ban", "unban"}:
                target_user_id = int(parts[2])
                services.admin.set_blocked(
                    admin=admin,
                    target_user_id=target_user_id,
                    is_blocked=action == "ban",
                )
                card = services.admin.user_card(target_user_id)
                await _show_screen(
                    callback.message,
                    services,
                    user["id"],
                    _format_admin_user_card(card),
                    reply_markup=admin_user_card_keyboard(
                        card, can_manage=services.admin.can_manage(admin)
                    ),
                    delete_current=True,
                )
            elif action == "credit":
                if not services.admin.can_manage(admin):
                    raise BusinessRuleError("Недостаточно прав")
                target_user_id = int(parts[2])
                _set_dialog_state(
                    services,
                    user["id"],
                    state="admin_waiting_credit",
                    payload={"target_user_id": target_user_id},
                )
                await _show_screen(
                    callback.message,
                    services,
                    user["id"],
                    "Введите положительное целое число coins для начисления.",
                    reply_markup=admin_back_keyboard(),
                    delete_current=True,
                )
            elif action == "search":
                _set_dialog_state(
                    services,
                    user["id"],
                    state="admin_waiting_search",
                    payload={},
                )
                await _show_screen(
                    callback.message,
                    services,
                    user["id"],
                    "Введите @username, Telegram ID или внутренний user ID.",
                    reply_markup=admin_back_keyboard(),
                    delete_current=True,
                )
            else:
                await callback.answer("Неизвестное действие", show_alert=True)
                return
        except (BusinessRuleError, NotFoundError, ValueError) as exc:
            await _show_screen(
                callback.message,
                services,
                user["id"],
                str(exc),
                reply_markup=admin_back_keyboard(),
                delete_current=True,
            )
        await callback.answer()

    @router.callback_query(F.data == "menu:home")
    async def menu_home(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        _clear_dialog_state(services, user["id"])
        if callback.message:
            await _send_main_menu(
                callback.message, services, user["id"], delete_current=True
            )
        await callback.answer()

    @router.callback_query(F.data == "menu:main")
    async def menu_main(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        _clear_dialog_state(services, user["id"])
        if callback.message:
            await _send_menu_screen(
                callback.message, services, user["id"], delete_current=True
            )
        await callback.answer()

    @router.callback_query(F.data == "menu:balance")
    async def menu_balance(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        if callback.message:
            await _send_balance(
                callback.message, services, user["id"], delete_current=True
            )
        await callback.answer()

    @router.callback_query(F.data == "menu:plans")
    async def menu_plans(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        if callback.message:
            await _send_plans(
                callback.message, services, user["id"], delete_current=True
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("buy:"))
    async def buy_plan(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        plan_code = callback.data.split(":", 1)[1] if callback.data else ""
        try:
            payment = services.payments.create_mock_payment(
                user_id=user["id"], plan_code=plan_code
            )
        except NotFoundError as exc:
            if callback.message:
                await _show_screen(
                    callback.message,
                    services,
                    user["id"],
                    str(exc),
                    reply_markup=back_to_menu_keyboard(),
                    delete_current=True,
                )
            await callback.answer()
            return

        _set_dialog_state(
            services,
            user["id"],
            state="waiting_mock_payment",
            payload={"payment_id": payment["id"]},
        )
        if callback.message:
            await _show_screen(
                callback.message,
                services,
                user["id"],
                "Тестовый платеж создан со статусом pending.\n"
                "Нажмите кнопку ниже, чтобы имитировать успешный webhook.",
                reply_markup=payment_keyboard(payment["id"], payment["payment_url"]),
                delete_current=True,
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("pay:"))
    async def pay_mock(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        payment_id = int(callback.data.split(":", 1)[1]) if callback.data else 0
        result = services.payments.process_mock_success_webhook_for_payment_id(
            payment_id=payment_id
        )
        _clear_dialog_state(services, user["id"])
        if result.processed:
            text = (
                "Оплата прошла успешно.\n"
                f"Начислено: {result.credited_coins} coins.\n"
                f"Текущий баланс: {result.subscription['coins_balance_cache']} coins."
            )
        else:
            text = "Этот mock-webhook уже был обработан. Повторного начисления нет."
        if callback.message:
            await _show_screen(
                callback.message,
                services,
                user["id"],
                text,
                reply_markup=main_menu_keyboard(),
                delete_current=True,
            )
        await callback.answer()

    @router.callback_query(F.data == "menu:models")
    async def menu_models(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        _clear_dialog_state(services, user["id"])
        models = services.catalog.list_models()
        if callback.message:
            await _show_screen(
                callback.message,
                services,
                user["id"],
                _format_models(models),
                reply_markup=models_keyboard(models),
                delete_current=True,
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("model:"))
    async def choose_model(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        model_id = int(callback.data.split(":", 1)[1]) if callback.data else 0
        model = services.catalog.get_model(model_id)
        if model is None:
            if callback.message:
                await _show_screen(
                    callback.message,
                    services,
                    user["id"],
                    "Модель не найдена. Выберите нейросетку заново.",
                    reply_markup=main_menu_keyboard(),
                    delete_current=True,
                )
            await callback.answer()
            return
        if model["generation_type"] == "text":
            if callback.message:
                await _send_text_chat_screen(
                    callback.message,
                    services,
                    user["id"],
                    model=model,
                    delete_current=True,
                )
            await callback.answer()
            return
        _set_dialog_state(
            services,
            user["id"],
            state="waiting_prompt",
            payload={"model_price_id": model_id},
        )
        if callback.message:
            await _show_screen(
                callback.message,
                services,
                user["id"],
                "Отправьте prompt для выбранной модели.",
                reply_markup=back_to_menu_keyboard(),
                delete_current=True,
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("text_chat:"))
    async def text_chat_callback(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        if not callback.message or not callback.data:
            await callback.answer()
            return

        session_state, session_payload = _session_state_payload(services, user["id"])
        if session_state not in {"waiting_text_chat_choice", "waiting_text_chat_prompt"}:
            await callback.answer()
            return

        model_price_id = int(session_payload.get("model_price_id", 0))
        current_chat_id = int(session_payload.get("current_text_chat_id", 0))
        model = services.catalog.get_model(model_price_id)
        if model is None:
            _clear_dialog_state(services, user["id"])
            await _show_screen(
                callback.message,
                services,
                user["id"],
                "Модель не найдена. Выберите нейросетку заново.",
                reply_markup=main_menu_keyboard(),
                delete_current=True,
            )
            await callback.answer()
            return

        action_parts = callback.data.split(":")
        action = action_parts[1] if len(action_parts) > 1 else ""

        if action == "back":
            await _send_text_chat_screen(
                callback.message,
                services,
                user["id"],
                model=model,
                current_chat_id=current_chat_id if current_chat_id > 0 else None,
                delete_current=True,
            )
            await callback.answer()
            return

        if action == "add":
            _set_dialog_state(
                services,
                user["id"],
                state="waiting_text_chat_name",
                payload={
                    "model_price_id": model_price_id,
                    "current_text_chat_id": current_chat_id,
                },
            )
            await _show_screen(
                callback.message,
                services,
                user["id"],
                "Введите название нового чата.",
                reply_markup=ReplyKeyboardRemove(),
                delete_current=True,
            )
            await callback.answer()
            return

        if action == "delete":
            if current_chat_id <= 0:
                await _send_text_chat_screen(
                    callback.message,
                    services,
                    user["id"],
                    model=model,
                    notice="Сначала откройте чат, который хотите удалить.",
                    delete_current=True,
                )
                await callback.answer()
                return
            try:
                fallback = services.text_chats.delete(
                    user_id=user["id"], chat_id=current_chat_id
                )
                await _send_text_chat_screen(
                    callback.message,
                    services,
                    user["id"],
                    model=model,
                    current_chat_id=int(fallback["id"]),
                    notice="Чат удалён.",
                    delete_current=True,
                )
            except BusinessRuleError as exc:
                await _send_text_chat_screen(
                    callback.message,
                    services,
                    user["id"],
                    model=model,
                    current_chat_id=current_chat_id,
                    notice=str(exc),
                    delete_current=True,
                )
            await callback.answer()
            return

        if action == "select" and len(action_parts) > 2:
            chat_id = int(action_parts[2])
            try:
                chat = services.text_chats.get_active(
                    user_id=user["id"], chat_id=chat_id
                )
            except NotFoundError:
                await callback.answer("Чат не найден.", show_alert=True)
                return
            await _send_text_chat_prompt_screen(
                callback.message,
                services,
                user["id"],
                model=model,
                current_chat=chat,
                notice=f"Чат «{chat['title']}» выбран.",
                delete_current=True,
            )
            await callback.answer()
            return

        await callback.answer()

    @router.callback_query(F.data == "menu:history")
    async def menu_history(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        if callback.message:
            await _send_history(
                callback.message, services, user["id"], delete_current=True
            )
        await callback.answer()

    @router.callback_query(F.data == "menu:support")
    async def menu_support(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        if callback.message:
            await _send_support(
                callback.message, services, user["id"], delete_current=True
            )
        await callback.answer()

    @router.callback_query(F.data == "menu:referral")
    async def menu_referral(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        if callback.message:
            await _show_screen(
                callback.message,
                services,
                user["id"],
                "🤝 Реферальная программа пока ещё не готова.",
                reply_markup=profile_keyboard(),
                delete_current=True,
            )
        await callback.answer()

    @router.message()
    async def prompt_or_fallback(message: Message) -> None:
        _record_message("prompt_or_fallback", message)
        await _delete_user_message(message)
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        if _is_start_text(message.text):
            if _is_blocked_regular_user(services, user):
                await _send_blocked_notice(message, services, user["id"])
                return
            _reset_dialog_state(services, user["id"])
            await _send_onboarding_greeting(
                message, services, user["id"], delete_current=True
            )
            return

        session = services.users.get_session(user["id"])
        if session and session["state"] in {"admin_waiting_search", "admin_waiting_credit"}:
            admin = services.admin.ensure_admin_access(user)
            if not admin:
                _clear_dialog_state(services, user["id"])
                return
            payload = loads_dict(session.get("payload"))
            text = (message.text or "").strip()
            if session["state"] == "admin_waiting_search":
                target = services.admin.find_user(text)
                _clear_dialog_state(services, user["id"])
                if target is None:
                    await _show_screen(
                        message,
                        services,
                        user["id"],
                        "Пользователь не найден.",
                        reply_markup=admin_back_keyboard(),
                        delete_current=True,
                    )
                    return
                card = services.admin.user_card(target["id"])
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    _format_admin_user_card(card),
                    reply_markup=admin_user_card_keyboard(
                        card, can_manage=services.admin.can_manage(admin)
                    ),
                    delete_current=True,
                )
                return

            target_user_id = int(payload.get("target_user_id", 0))
            try:
                amount = int(text)
                balance = services.admin.manual_credit(
                    admin=admin, target_user_id=target_user_id, amount=amount
                )
                _clear_dialog_state(services, user["id"])
                card = services.admin.user_card(target_user_id)
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    f"Начислено {amount} coins. Новый баланс: {balance} coins.\n\n"
                    f"{_format_admin_user_card(card)}",
                    reply_markup=admin_user_card_keyboard(
                        card, can_manage=services.admin.can_manage(admin)
                    ),
                    delete_current=True,
                )
            except ValueError:
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    "Введите положительное целое число.",
                    reply_markup=admin_back_keyboard(),
                    delete_current=True,
                )
            except (BusinessRuleError, NotFoundError) as exc:
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    str(exc),
                    reply_markup=admin_back_keyboard(),
                    delete_current=True,
                )
            return

        if _is_blocked_regular_user(services, user):
            await _send_blocked_notice(message, services, user["id"])
            return

        if await _handle_reply_menu(message, services, user):
            return

        session = services.users.get_session(user["id"])
        if session and session["state"] == "waiting_text_chat_choice":
            payload = loads_dict(session.get("payload"))
            model_price_id = int(payload.get("model_price_id", 0))
            current_chat_id = int(payload.get("current_text_chat_id", 0))
            model = services.catalog.get_model(model_price_id)
            if model is None:
                _clear_dialog_state(services, user["id"])
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    "Модель не найдена. Выберите нейросетку заново.",
                    reply_markup=main_menu_keyboard(),
                    delete_current=True,
                )
                return
            await _send_text_chat_screen(
                message,
                services,
                user["id"],
                model=model,
                current_chat_id=current_chat_id,
                notice="Выберите чат на нижней клавиатуре.",
                delete_current=True,
            )
            return

        if session and session["state"] == "waiting_text_chat_prompt":
            payload = loads_dict(session.get("payload"))
            model_price_id = int(payload.get("model_price_id", 0))
            current_chat_id = int(payload.get("current_text_chat_id", 0))
            model = services.catalog.get_model(model_price_id)
            if model is None:
                _clear_dialog_state(services, user["id"])
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    "Модель не найдена. Выберите нейросетку заново.",
                    reply_markup=main_menu_keyboard(),
                    delete_current=True,
                )
                return
            try:
                current_chat = services.text_chats.get_active(
                    user_id=user["id"], chat_id=current_chat_id
                )
            except NotFoundError:
                current_chat = services.text_chats.default_for_model(
                    user_id=user["id"], model_price_id=model_price_id
                )
                current_chat_id = int(current_chat["id"])
                chats = services.text_chats.list_for_model(
                    user_id=user["id"], model_price_id=model_price_id
                )
                _set_dialog_state(
                    services,
                    user["id"],
                    state="waiting_text_chat_prompt",
                    payload=_text_chat_payload(model, chats, current_chat),
                )

            chat_keyboard = text_chat_prompt_keyboard()
            prompt_text = message.text or ""
            if not prompt_text.strip():
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    "Отправьте текстовый вопрос.",
                    reply_markup=chat_keyboard,
                    delete_current=True,
                )
                return

            await _show_screen(
                message,
                services,
                user["id"],
                "Запускаю генерацию...",
                reply_markup=chat_keyboard,
                delete_current=True,
            )
            try:
                generation = services.generations.generate(
                    user_id=user["id"],
                    model_price_id=model_price_id,
                    prompt_text=prompt_text,
                    text_chat_id=current_chat_id,
                    text_chat_title=str(current_chat["title"]),
                    text_chat_system_prompt=str(current_chat.get("system_prompt") or ""),
                )
            except NoActiveSubscriptionError:
                _clear_dialog_state(services, user["id"])
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    "Нужна активная подписка. Откройте тарифы и оплатите тестово.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            except InsufficientCoinsError:
                _clear_dialog_state(services, user["id"])
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    "Недостаточно coins для этой модели. Выберите тариф или модель дешевле.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            except GenerationProviderFailedError:
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    "Не получилось выполнить генерацию. Coins возвращены.",
                    reply_markup=chat_keyboard,
                )
                return
            except NotFoundError as exc:
                _clear_dialog_state(services, user["id"])
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    str(exc),
                    reply_markup=main_menu_keyboard(),
                )
                return

            await _show_screen(
                message,
                services,
                user["id"],
                _format_generation_result(generation.result),
                reply_markup=chat_keyboard,
            )
            return

        if session and session["state"] == "waiting_model_choice":
            payload = loads_dict(session.get("payload"))
            choices = payload.get("model_choices")
            model_ids = choices.values() if isinstance(choices, dict) else []
            models = [
                model
                for model_id in model_ids
                if (model := services.catalog.get_model(int(model_id))) is not None
            ]
            await _show_screen(
                message,
                services,
                user["id"],
                "Выберите модель на нижней клавиатуре.",
                reply_markup=models_keyboard(models) if models else main_menu_keyboard(),
                delete_current=True,
            )
            return

        if not session or session["state"] != "waiting_prompt":
            await _show_screen(
                message,
                services,
                user["id"],
                "Выберите действие на нижней клавиатуре.",
                reply_markup=main_menu_keyboard(),
                delete_current=True,
            )
            return

        payload = loads_dict(session.get("payload"))
        model_price_id = int(payload.get("model_price_id", 0))
        prompt_text = message.text or ""
        if not prompt_text.strip():
            await _show_screen(
                message,
                services,
                user["id"],
                "Отправьте текстовый prompt.",
                reply_markup=back_to_menu_keyboard(),
                delete_current=True,
            )
            return

        await _show_screen(
            message,
            services,
            user["id"],
            "Запускаю генерацию...",
            reply_markup=back_to_menu_keyboard(),
            delete_current=True,
        )
        try:
            generation = services.generations.generate(
                user_id=user["id"],
                model_price_id=model_price_id,
                prompt_text=prompt_text,
            )
        except NoActiveSubscriptionError:
            _clear_dialog_state(services, user["id"])
            await _show_screen(
                message,
                services,
                user["id"],
                "Нужна активная подписка. Откройте тарифы и оплатите тестово.",
                reply_markup=back_to_menu_keyboard(),
            )
            return
        except InsufficientCoinsError:
            _clear_dialog_state(services, user["id"])
            await _show_screen(
                message,
                services,
                user["id"],
                "Недостаточно coins для этой модели. Выберите тариф или модель дешевле.",
                reply_markup=back_to_menu_keyboard(),
            )
            return
        except GenerationProviderFailedError:
            _clear_dialog_state(services, user["id"])
            await _show_screen(
                message,
                services,
                user["id"],
                "Не получилось выполнить генерацию. Coins возвращены.",
                reply_markup=back_to_menu_keyboard(),
            )
            return
        except NotFoundError as exc:
            _clear_dialog_state(services, user["id"])
            await _show_screen(
                message,
                services,
                user["id"],
                str(exc),
                reply_markup=back_to_menu_keyboard(),
            )
            return

        await _show_screen(
            message,
            services,
            user["id"],
            _format_generation_result(generation.result),
            reply_markup=back_to_menu_keyboard(),
        )

    return router
