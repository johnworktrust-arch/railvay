from __future__ import annotations

from typing import Any, Dict

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from ceai.bot.keyboards import (
    back_to_menu_keyboard,
    main_menu_keyboard,
    models_keyboard,
    payment_keyboard,
    plans_keyboard,
)
from ceai.json_utils import loads_dict
from ceai.services.app import AppServices
from ceai.services.exceptions import (
    GenerationProviderFailedError,
    InsufficientCoinsError,
    NoActiveSubscriptionError,
    NotFoundError,
)


def _user_kwargs(message_or_callback: Message | CallbackQuery) -> Dict[str, Any]:
    from_user = message_or_callback.from_user
    return {
        "telegram_id": from_user.id,
        "username": from_user.username,
        "first_name": from_user.first_name,
        "last_name": from_user.last_name,
        "language_code": from_user.language_code,
    }


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
        "CeaAI MVP\n\n"
        f"Баланс: {balance} coins\n"
        f"{sub_line}\n\n"
        "Выберите действие."
    )


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


async def _send_main_menu(message: Message, services: AppServices, user_id: int) -> None:
    subscription = services.subscriptions.active_for_user(user_id)
    await message.answer(
        _format_menu(subscription), reply_markup=main_menu_keyboard()
    )


def create_router(services: AppServices) -> Router:
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        services.users.clear_session(user["id"])
        await message.answer(
            "Добро пожаловать в CeaAI MVP. Здесь все AI и платежи пока работают на mock-заглушках."
        )
        await _send_main_menu(message, services, user["id"])

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        services.users.clear_session(user["id"])
        await message.answer(
            "Поддержка CeaAI MVP\n\n"
            "Команды: /start, /help.\n"
            "Чтобы проверить сценарий, откройте тарифы, оплатите тестово, "
            "выберите AI-инструмент и отправьте prompt.\n"
            "Для тестовой ошибки провайдера отправьте prompt с текстом mock_error.",
            reply_markup=back_to_menu_keyboard(),
        )

    @router.callback_query(F.data == "menu:home")
    async def menu_home(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        services.users.clear_session(user["id"])
        if callback.message:
            await _send_main_menu(callback.message, services, user["id"])
        await callback.answer()

    @router.callback_query(F.data == "menu:balance")
    async def menu_balance(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        subscription = services.subscriptions.active_for_user(user["id"])
        if subscription:
            text = (
                f"Баланс: {subscription['coins_balance_cache']} coins\n"
                f"Подписка: {subscription['plan_name']} до {subscription['ends_at'][:10]}"
            )
        else:
            text = "Активной подписки нет. Выберите тариф и оплатите тестово."
        if callback.message:
            await callback.message.answer(text, reply_markup=back_to_menu_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "menu:plans")
    async def menu_plans(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        services.users.set_session(user["id"], state="waiting_payment_choice")
        plans = services.catalog.list_plans()
        if callback.message:
            await callback.message.answer(
                _format_plans(plans), reply_markup=plans_keyboard(plans)
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("buy:"))
    async def buy_plan(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        plan_code = callback.data.split(":", 1)[1] if callback.data else ""
        try:
            payment = services.payments.create_mock_payment(
                user_id=user["id"], plan_code=plan_code
            )
        except NotFoundError as exc:
            if callback.message:
                await callback.message.answer(str(exc), reply_markup=back_to_menu_keyboard())
            await callback.answer()
            return

        services.users.set_session(
            user["id"],
            state="waiting_mock_payment",
            payload={"payment_id": payment["id"]},
        )
        if callback.message:
            await callback.message.answer(
                "Тестовый платеж создан со статусом pending.\n"
                "Нажмите кнопку ниже, чтобы имитировать успешный webhook.",
                reply_markup=payment_keyboard(payment["id"], payment["payment_url"]),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("pay:"))
    async def pay_mock(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        payment_id = int(callback.data.split(":", 1)[1]) if callback.data else 0
        result = services.payments.process_mock_success_webhook_for_payment_id(
            payment_id=payment_id
        )
        services.users.clear_session(user["id"])
        if result.processed:
            text = (
                "Оплата прошла успешно.\n"
                f"Начислено: {result.credited_coins} coins.\n"
                f"Текущий баланс: {result.subscription['coins_balance_cache']} coins."
            )
        else:
            text = "Этот mock-webhook уже был обработан. Повторного начисления нет."
        if callback.message:
            await callback.message.answer(text, reply_markup=main_menu_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "menu:models")
    async def menu_models(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        services.users.clear_session(user["id"])
        models = services.catalog.list_models()
        if callback.message:
            await callback.message.answer(
                _format_models(models), reply_markup=models_keyboard(models)
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("model:"))
    async def choose_model(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        model_id = int(callback.data.split(":", 1)[1]) if callback.data else 0
        services.users.set_session(
            user["id"], state="waiting_prompt", payload={"model_price_id": model_id}
        )
        if callback.message:
            await callback.message.answer("Отправьте prompt для выбранной модели.")
        await callback.answer()

    @router.callback_query(F.data == "menu:history")
    async def menu_history(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        rows = services.generations.list_recent(user_id=user["id"], limit=10)
        if callback.message:
            await callback.message.answer(
                _format_history(rows), reply_markup=back_to_menu_keyboard()
            )
        await callback.answer()

    @router.callback_query(F.data == "menu:support")
    async def menu_support(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        services.users.clear_session(user["id"])
        if callback.message:
            await callback.message.answer(
                "Поддержка MVP: напишите владельцу проекта или используйте /help.\n"
                "Все платежи и AI-провайдеры сейчас работают в mock-режиме.",
                reply_markup=back_to_menu_keyboard(),
            )
        await callback.answer()

    @router.message()
    async def prompt_or_fallback(message: Message) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        session = services.users.get_session(user["id"])
        if not session or session["state"] != "waiting_prompt":
            await message.answer(
                "Выберите действие в меню.", reply_markup=main_menu_keyboard()
            )
            return

        payload = loads_dict(session.get("payload"))
        model_price_id = int(payload.get("model_price_id", 0))
        prompt_text = message.text or ""
        if not prompt_text.strip():
            await message.answer("Отправьте текстовый prompt.")
            return

        await message.answer("Запускаю mock-генерацию...")
        try:
            generation = services.generations.generate(
                user_id=user["id"],
                model_price_id=model_price_id,
                prompt_text=prompt_text,
            )
        except NoActiveSubscriptionError:
            services.users.clear_session(user["id"])
            await message.answer(
                "Нужна активная подписка. Откройте тарифы и оплатите тестово.",
                reply_markup=main_menu_keyboard(),
            )
            return
        except InsufficientCoinsError:
            services.users.clear_session(user["id"])
            await message.answer(
                "Недостаточно coins для этой модели. Выберите тариф или модель дешевле.",
                reply_markup=main_menu_keyboard(),
            )
            return
        except GenerationProviderFailedError:
            services.users.clear_session(user["id"])
            await message.answer(
                "Не получилось выполнить mock-генерацию. Coins возвращены.",
                reply_markup=main_menu_keyboard(),
            )
            return
        except NotFoundError as exc:
            services.users.clear_session(user["id"])
            await message.answer(str(exc), reply_markup=main_menu_keyboard())
            return

        services.users.clear_session(user["id"])
        await message.answer(
            _format_generation_result(generation.result, generation.balance_after),
            reply_markup=main_menu_keyboard(),
        )

    return router
