from __future__ import annotations

from typing import Any, Dict

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from ceai.bot.keyboards import (
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
    models_keyboard,
    payment_keyboard,
    plans_keyboard,
)
from ceai.formatting import format_datetime_minute
from ceai.json_utils import loads_dict
from ceai.services.app import AppServices
from ceai.services.exceptions import (
    BusinessRuleError,
    GenerationProviderFailedError,
    InsufficientCoinsError,
    NoActiveSubscriptionError,
    NotFoundError,
)


LAST_BOT_MESSAGE_ID = "last_bot_message_id"


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


def _set_dialog_state(
    services: AppServices,
    user_id: int,
    *,
    state: str,
    payload: Dict[str, Any] | None = None,
) -> None:
    _, current_payload = _session_state_payload(services, user_id)
    next_payload = dict(payload or {})
    if LAST_BOT_MESSAGE_ID in current_payload and LAST_BOT_MESSAGE_ID not in next_payload:
        next_payload[LAST_BOT_MESSAGE_ID] = current_payload[LAST_BOT_MESSAGE_ID]
    services.users.set_session(user_id, state=state, payload=next_payload)


def _clear_dialog_state(services: AppServices, user_id: int) -> None:
    _set_dialog_state(services, user_id, state="idle", payload={})


async def _delete_message(message: Message, message_id: int) -> None:
    try:
        await message.bot.delete_message(chat_id=message.chat.id, message_id=message_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


async def _delete_current_message(message: Message) -> None:
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


async def _show_screen(
    message: Message,
    services: AppServices,
    user_id: int,
    text: str,
    *,
    reply_markup: Any | None = None,
    delete_current: bool = False,
) -> Message:
    state, payload = _session_state_payload(services, user_id)
    last_message_id = payload.get(LAST_BOT_MESSAGE_ID)
    if isinstance(last_message_id, int):
        await _delete_message(message, last_message_id)
    if delete_current:
        await _delete_current_message(message)

    sent = await message.bot.send_message(
        chat_id=message.chat.id,
        text=text,
        reply_markup=reply_markup,
    )
    payload[LAST_BOT_MESSAGE_ID] = sent.message_id
    services.users.set_session(user_id, state=state, payload=payload)
    return sent


def _format_menu(subscription: Dict[str, Any] | None) -> str:
    if subscription:
        balance = subscription["coins_balance_cache"]
        plan = subscription["plan_name"]
        ends_at = subscription["ends_at"][:10]
        sub_line = f"Подписка: {plan} до {ends_at}"
    else:
        balance = 0
        sub_line = "Подписка: нет активной"
    return (
        "Профиль CeaAI\n\n"
        f"Баланс: {balance} coins\n"
        f"{sub_line}\n\n"
        "Выберите действие на нижней клавиатуре."
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
    lines = ["Выберите AI-инструмент:"]
    for model in models:
        lines.append(
            f"{model['display_name']} — {model['generation_type']} — "
            f"{model['coins_cost']} coins"
        )
    return "\n".join(lines)


def _format_generation_result(result: Dict[str, Any], balance_after: int) -> str:
    kind = result.get("kind")
    if kind == "text":
        body = str(result.get("text", ""))
    elif kind in {"image", "video"}:
        body = f"{result.get('caption', 'Mock result')}\n{result.get('url')}"
    elif kind == "tts":
        body = f"{result.get('message', 'Mock TTS result')}\n{result.get('url')}"
    else:
        body = str(result)
    return f"{body}\n\nБаланс после генерации: {balance_after} coins"


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
    text = _format_menu(subscription)
    if intro:
        text = f"{intro}\n\n{text}"
    await _show_screen(
        message,
        services,
        user_id,
        text,
        reply_markup=main_menu_keyboard(),
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
    await _show_screen(
        message,
        services,
        user_id,
        "Поддержка MVP: напишите владельцу проекта или используйте /help.\n"
        "Все платежи и AI-провайдеры сейчас работают в mock-режиме.",
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
    _clear_dialog_state(services, user_id)
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
    await _show_screen(
        message,
        services,
        user_id,
        f"{title}\n\n{_format_models(models)}",
        reply_markup=models_keyboard(models),
        delete_current=delete_current,
    )


async def _handle_reply_menu(
    message: Message, services: AppServices, user: Dict[str, Any]
) -> bool:
    text = (message.text or "").strip()
    text_lower = text.casefold()

    if text_lower in {"menu", "/menu", "главное меню"}:
        _clear_dialog_state(services, user["id"])
        await _send_menu_screen(message, services, user["id"], delete_current=True)
        return True

    if text_lower in {
        "старт",
        "start",
        "профиль",
        "/профиль",
        "profile",
        "/profile",
    } or text == PROFILE_BUTTON:
        _clear_dialog_state(services, user["id"])
        intro = None
        if text_lower in {"старт", "start"}:
            intro = "Добро пожаловать в CeaAI MVP. Открываю профиль."
        await _send_main_menu(
            message, services, user["id"], intro=intro, delete_current=True
        )
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

    @router.message(Command("admin"))
    async def admin_command(message: Message) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        admin = services.admin.ensure_admin_access(user)
        if not admin:
            return
        _clear_dialog_state(services, user["id"])
        await _send_admin_home(message, services, user["id"], delete_current=True)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        if _is_blocked_regular_user(services, user):
            await _send_blocked_notice(message, services, user["id"])
            return
        _clear_dialog_state(services, user["id"])
        await _send_main_menu(
            message,
            services,
            user["id"],
            intro="Добро пожаловать в CeaAI MVP. Здесь все AI и платежи пока работают на mock-заглушках.",
            delete_current=True,
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        if _is_blocked_regular_user(services, user):
            await _send_blocked_notice(message, services, user["id"])
            return
        await _send_support(message, services, user["id"], delete_current=True)

    @router.message(Command("menu"))
    async def menu_command(message: Message) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        if _is_blocked_regular_user(services, user):
            await _send_blocked_notice(message, services, user["id"])
            return
        _clear_dialog_state(services, user["id"])
        await _send_menu_screen(message, services, user["id"], delete_current=True)

    @router.message(Command("profile"))
    async def profile_command(message: Message) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        if _is_blocked_regular_user(services, user):
            await _send_blocked_notice(message, services, user["id"])
            return
        _clear_dialog_state(services, user["id"])
        await _send_main_menu(message, services, user["id"], delete_current=True)

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
                reply_markup=main_menu_keyboard(),
                delete_current=True,
            )
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

    @router.message()
    async def prompt_or_fallback(message: Message) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
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
                reply_markup=main_menu_keyboard(),
                delete_current=True,
            )
            return

        await _show_screen(
            message,
            services,
            user["id"],
            "Запускаю mock-генерацию...",
            reply_markup=main_menu_keyboard(),
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
            _clear_dialog_state(services, user["id"])
            await _show_screen(
                message,
                services,
                user["id"],
                "Не получилось выполнить mock-генерацию. Coins возвращены.",
                reply_markup=main_menu_keyboard(),
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

        _clear_dialog_state(services, user["id"])
        await _show_screen(
            message,
            services,
            user["id"],
            _format_generation_result(generation.result, generation.balance_after),
            reply_markup=main_menu_keyboard(),
        )

    return router
