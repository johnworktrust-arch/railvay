from __future__ import annotations

import asyncio
import base64
import binascii
import io
from html import escape
from pathlib import Path
from typing import Any, Dict

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    BufferedInputFile,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
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
    crystal_packages_keyboard,
    inline_back_to_menu_keyboard,
    main_menu_button_keyboard,
    main_menu_keyboard,
    model_choice_label,
    models_keyboard,
    onboarding_continue_keyboard,
    onboarding_links_keyboard,
    payment_keyboard,
    payment_methods_keyboard,
    plans_keyboard,
    profile_keyboard,
    referral_keyboard,
    subscription_required_keyboard,
    text_chat_keyboard,
    text_chat_label,
    text_chat_prompt_keyboard,
)
from ceai.config import DEFAULT_PUBLIC_OFFER_URL
from ceai.formatting import (
    format_coin_amount,
    format_datetime_minute,
    format_datetime_russian_minute,
)
from ceai.json_utils import loads_dict
from ceai.pricing import telegram_stars_amount_for_rub
from ceai.providers.base import ImageInput
from ceai.runtime_diagnostics import record_error, record_message
from ceai.services.app import AppServices
from ceai.services.exceptions import (
    BusinessRuleError,
    GenerationProviderFailedError,
    InsufficientCoinsError,
    NoActiveSubscriptionError,
    NotFoundError,
)
from ceai.services.referrals import (
    REFERRAL_RATE_PERCENT,
    REFERRAL_WITHDRAWAL_MIN_KOPECKS,
    ReferralApplyResult,
    format_rubles_from_kopecks,
)


LAST_BOT_MESSAGE_ID = "last_bot_message_id"
LAST_BOT_MESSAGE_IDS = "last_bot_message_ids"
LAST_REPLY_KEYBOARD_SIGNATURE = "last_reply_keyboard_signature"
TELEGRAM_STARS_INVOICE_MESSAGE_ID = "telegram_stars_invoice_message_id"
START_TEXT_ALIASES = {"старт", "/старт", "start", "/start", "начать"}
ONBOARDING_PROMO_IMAGE_PATH = (
    Path(__file__).resolve().parents[1] / "assets" / "onboarding_promo.jpeg"
)
MAX_IMAGE_INPUT_BYTES = 20 * 1024 * 1024
DEFAULT_IMAGE_EDIT_PROMPT = "Улучши изображение, сохранив основной сюжет."


def _is_start_text(text: str | None) -> bool:
    return (text or "").strip().casefold() in START_TEXT_ALIASES


def _is_user_message(message: Message) -> bool:
    from_user = getattr(message, "from_user", None)
    return bool(from_user and not from_user.is_bot)


async def _image_input_from_message(message: Message) -> ImageInput | None:
    file_id: str | None = None
    mime_type = "image/jpeg"
    file_name = "telegram-photo.jpg"

    if message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
    elif message.document and str(message.document.mime_type or "").startswith("image/"):
        document = message.document
        file_id = document.file_id
        mime_type = str(document.mime_type or "image/png")
        file_name = str(document.file_name or "telegram-image")

    if not file_id:
        return None

    file = await message.bot.get_file(file_id)
    if not file.file_path:
        raise ValueError("Не получилось получить файл изображения.")

    buffer = io.BytesIO()
    await message.bot.download_file(file.file_path, destination=buffer)
    data = buffer.getvalue()
    if len(data) > MAX_IMAGE_INPUT_BYTES:
        raise ValueError("Изображение слишком большое. Отправьте файл до 20 МБ.")
    if not data:
        raise ValueError("Изображение не загрузилось. Попробуйте отправить его ещё раз.")

    return ImageInput(data=data, mime_type=mime_type, file_name=file_name)


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
    else:
        payload.pop(LAST_REPLY_KEYBOARD_SIGNATURE, None)
    services.users.set_session(user_id, state=state, payload=payload)


def _track_existing_screen_message(
    services: AppServices, user_id: int, message: Message
) -> None:
    if not message.message_id:
        return
    state, payload = _session_state_payload(services, user_id)
    _remember_screen_message(
        services,
        user_id,
        state=state,
        payload=payload,
        message_id=message.message_id,
        reply_markup=message.reply_markup,
    )


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


async def _delete_telegram_stars_invoice_message(
    message: Message, services: AppServices, user_id: int
) -> None:
    _, payload = _session_state_payload(services, user_id)
    invoice_message_id = payload.get(TELEGRAM_STARS_INVOICE_MESSAGE_ID)
    if not isinstance(invoice_message_id, int):
        return
    try:
        await message.bot.delete_message(
            chat_id=message.chat.id,
            message_id=invoice_message_id,
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
    return await message.bot.send_message(
        chat_id=message.chat.id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )


async def _remove_legacy_reply_keyboard(
    message: Message, payload: Dict[str, Any], reply_markup: Any | None
) -> None:
    if LAST_REPLY_KEYBOARD_SIGNATURE not in payload:
        return
    if isinstance(reply_markup, (ReplyKeyboardMarkup, ReplyKeyboardRemove)):
        return
    try:
        sent = await message.bot.send_message(
            chat_id=message.chat.id,
            text="Клавиатура обновлена.",
            reply_markup=ReplyKeyboardRemove(),
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        payload.pop(LAST_REPLY_KEYBOARD_SIGNATURE, None)
        return
    try:
        await message.bot.delete_message(
            chat_id=message.chat.id,
            message_id=sent.message_id,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    payload.pop(LAST_REPLY_KEYBOARD_SIGNATURE, None)


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
    await _remove_legacy_reply_keyboard(message, payload, reply_markup)
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
    promo_keyboard = onboarding_links_keyboard(
        info_channel_url=services.settings.info_channel_url,
        support_username=services.settings.support_username,
    )
    try:
        await message.bot.send_photo(
            chat_id=message.chat.id,
            photo=FSInputFile(ONBOARDING_PROMO_IMAGE_PATH),
            caption=_format_onboarding_promo(),
            reply_markup=promo_keyboard,
        )
    except (TelegramBadRequest, TelegramForbiddenError, FileNotFoundError):
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=_format_onboarding_promo(),
            reply_markup=promo_keyboard,
        )
    menu = await message.bot.send_message(
        chat_id=message.chat.id,
        text=_format_main_menu(),
        reply_markup=main_menu_keyboard(),
    )
    services.users.set_session(
        user_id,
        state="idle",
        payload={LAST_BOT_MESSAGE_IDS: [menu.message_id]},
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
        sub_line = f"⭐ Подписка: {escape(str(plan))}"
        expires_line = (
            "📅 Срок действия: "
            f"{escape(format_datetime_russian_minute(subscription.get('ends_at')))}"
        )
        auto_renew_line = (
            "🔁 Автопродление: включено"
            if subscription.get("auto_renew")
            else "🔁 Автопродление: выключено"
        )
    else:
        balance = 0
        sub_line = "⭐ Подписка: нет активной"
        expires_line = "📅 Срок действия: —"
        auto_renew_line = "🔁 Автопродление: —"
    invited_line = f"👥 Приглашено: {invited_users_count}"
    if invited_users_count <= 0:
        invited_line += (
            " (Приглашайте друзей и зарабатывайте 30% с каждого пополнения!)"
        )
    return (
        f"👤 Профиль: {_profile_link(user)}\n\n"
        f"ℹ️ ID: {user.get('telegram_id') or user.get('id')}\n"
        f"💰 Баланс: {format_coin_amount(balance)}\n"
        f"{sub_line}\n"
        f"{expires_line}\n"
        f"{auto_renew_line}\n\n"
        f"{invited_line}"
    )


def _referral_link(user: Dict[str, Any]) -> str:
    referral_code = str(
        user.get("referral_code")
        or f"tg{user.get('telegram_id') or user.get('id')}"
    ).strip()
    return f"https://t.me/aiceabot?start=ref_{referral_code}"


def _format_referral_screen(
    user: Dict[str, Any],
    *,
    invited_users_count: int,
    balance_kopecks: int = 0,
    withdrawal_method: str = "",
    requisites: str = "",
    rate_percent: int = REFERRAL_RATE_PERCENT,
    withdrawal_min_kopecks: int = REFERRAL_WITHDRAWAL_MIN_KOPECKS,
) -> str:
    referral_link = _referral_link(user)
    withdrawal_method_text = withdrawal_method.strip() or "не задан"
    requisites_text = requisites.strip() or "не указаны"
    withdrawal_min_text = format_rubles_from_kopecks(withdrawal_min_kopecks).replace(
        " ₽", "₽"
    )
    return (
        "👥 <b>Приглашайте друзей и зарабатывайте "
        f"{rate_percent}% с каждого пополнения!</b>\n\n"
        "Например:\n"
        "<blockquote>"
        "— Друзья перешли по вашей ссылке и потратили 1000₽\n"
        "— Вы получаете 300.0₽ и выводите на КАРТУ!"
        "</blockquote>\n\n"
        "📊 <b>Ваша статистика:</b>\n"
        "<blockquote>"
        f"— Приглашено: {invited_users_count}\n"
        f"— Баланс: {escape(format_rubles_from_kopecks(balance_kopecks))}\n"
        f"— Способ вывода: {escape(withdrawal_method_text)}\n"
        f"— Реквизиты: {escape(requisites_text)}"
        "</blockquote>\n\n"
        f"% <b>Текущая ставка: {rate_percent}%</b>\n"
        f"💼 Вывод доступен от {escape(withdrawal_min_text)}\n\n"
        "🔗 <b>Пригласительная ссылка:</b>\n"
        f"<code>{escape(referral_link)}</code>\n\n"
        "📨 Нажмите на ссылку, чтобы скопировать и поделиться с друзьями!"
    )


def _format_referral_withdrawal_unavailable(withdrawal_min_kopecks: int) -> str:
    minimum = format_rubles_from_kopecks(withdrawal_min_kopecks).replace(" ₽", " рублей")
    return (
        "❌ <b>Вывод средств сейчас недоступен.</b>\n\n"
        f"Вывод доступен при реферальном балансе от {escape(minimum)}."
    )


def _format_referral_withdrawal_available(
    *, support_username: str, balance_kopecks: int
) -> str:
    username = support_username.strip().lstrip("@") or "cea_help"
    return (
        "✅ <b>Вывод средств доступен.</b>\n\n"
        f"Ваш реферальный баланс: {escape(format_rubles_from_kopecks(balance_kopecks))}.\n"
        f"Для оформления заявки напишите в поддержку: @{escape(username)}."
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
        "В двух словах об основных инструментах чат-бота:\n\n"
        "Cea AI предоставляет доступ к самым современным и мощным "
        "AI-инструментам в одном Telegram-боте: текстовые нейросети, "
        "генерация фото, генерация видео и озвучка текста передовыми "
        "нейросетями.\n\n"
        "👇 Следите за обновлениями в нашем канале. Если возникнут вопросы "
        "или проблемы, обращайтесь в поддержку."
    )


def _format_main_menu() -> str:
    return "🏠 Главное меню\nВыберите нужный раздел 👇"


def _format_plans(plans: list[Dict[str, Any]]) -> str:
    return (
        "💳 Выберите тариф с подпиской.\n\n"
        "Нажмите на любой тариф ниже — покажу цену, количество коинов и что входит."
    )


def _format_crystal_packages() -> str:
    return "💳 Выберите количество коинов для покупки:"


def _format_payment_methods() -> str:
    return "💳 Выберите способ оплаты:"


def _format_plan_details(plan: Dict[str, Any]) -> str:
    code = str(plan.get("code") or "")
    coins = int(plan.get("coins_amount") or 0)
    price = int(plan.get("price_rub") or 0)
    chatgpt_requests = coins // 3
    deepseek_requests = coins
    image_requests = coins // 2
    stars_amount = telegram_stars_amount_for_rub(price)
    meta = {
        "start": {
            "icon": "⭐️",
            "label": "Старт",
            "tag": "для знакомства",
            "extra": "➕ Подходит, чтобы спокойно попробовать Cea AI",
        },
        "basic": {
            "icon": "🔥",
            "label": "Базовый",
            "tag": "популярный",
            "extra": "➕ Лучший вариант для регулярного общения с нейросетями",
        },
        "pro": {
            "icon": "⚡️",
            "label": "Про",
            "tag": "максимум",
            "extra": "➕ Больше всего коинов и запас для активной работы",
        },
    }.get(
        code,
        {
            "icon": "💳",
            "label": str(plan.get("name") or "Тариф"),
            "tag": "30 дней",
            "extra": "➕ Доступ к текстовым нейросетям Cea AI",
        },
    )
    return (
        f"{meta['icon']} {meta['label']} — {price} ₽\n"
        f"({meta['tag']})\n\n"
        "➕ DeepSeek, ChatGPT и GPT Image\n"
        f"➕ {format_coin_amount(coins)}\n"
        f"➕ До {deepseek_requests} запросов DeepSeek\n"
        f"➕ До {chatgpt_requests} запросов ChatGPT\n"
        f"➕ До {image_requests} изображений GPT Image\n"
        f"⭐ Telegram Stars: {stars_amount}⭐\n"
        "➕ Срок действия — 30 дней\n"
        f"{meta['extra']}\n\n"
        f"{_format_payment_methods()}"
    )


def _payment_method_label(payment_method: str) -> str:
    return {
        "card_sbp": "💳 Карта / СБП",
        "sbp": "💳 Карта / СБП",
        "usdt_trc20": "💵 Крипта USDT TRC20",
        "telegram_stars": "⭐️ Telegram Stars",
    }.get(payment_method, "оплата")


def _format_yookassa_payment_screen(
    plan: Dict[str, Any], *, public_offer_url: str
) -> str:
    price = int(plan.get("price_rub") or 0)
    coins = int(plan.get("coins_amount") or 0)
    duration_days = int(plan.get("duration_days") or 30)
    offer_url = public_offer_url.strip() or DEFAULT_PUBLIC_OFFER_URL
    return (
        f"💳 Стоимость выбранного тарифа — {price} ₽.\n\n"
        f"После оплаты вы получите {format_coin_amount(coins)}. "
        f"Доступ к тарифу действует {duration_days} дней.\n\n"
        "Подписка продлевается автоматически "
        f"ещё на {duration_days} дней за {price} ₽.\n\n"
        "Проверка платежа происходит автоматически. "
        "Коины начислятся на баланс сразу после подтверждения оплаты.\n\n"
        "Нажимая «Оплатить», вы подтверждаете согласие с условиями "
        "обработки данных и публичной офертой.\n\n"
        f"Публичная оферта: {offer_url}\n\n"
        "Отключить автоматическое продление можно в разделе "
        "«Профиль» → «Отключить автопродление»."
    )


def _subscription_required_message() -> str:
    return "Нужна активная подписка. Откройте тарифы и выберите подписку."


def _format_coin_unit(amount: Any) -> str:
    return f"{int(amount or 0)} Coin"


def _format_coin_balance_unit(amount: Any) -> str:
    return f"{int(amount or 0):.3f} Coin"


def _feature_temporarily_unavailable_message(feature_name: str) -> str:
    return (
        "❌ Функция временно недоступна.\n\n"
        f"Раздел «{feature_name}» сейчас находится в технической подготовке. "
        "Мы сообщим, когда он станет доступен."
    )


async def _send_telegram_stars_invoice(
    message: Message, payment: Dict[str, Any]
) -> Message:
    meta = loads_dict(payment.get("meta"))
    plan_name = str(meta.get("plan_name") or "Тариф CeaAI")
    coins_amount = int(meta.get("coins_amount") or 0)
    duration_days = int(meta.get("duration_days") or 30)
    stars_amount = int(meta.get("stars_amount") or payment["amount_rub"])
    coins_label = format_coin_amount(coins_amount)
    description = (
        f"Тариф «{plan_name}» на {duration_days} дней. "
        f"Включено: {coins_label}. "
        "Коины начислятся автоматически после оплаты."
        if coins_amount > 0
        else "Доступ к CeaAI. Коины начислятся автоматически после оплаты."
    )
    return await message.bot.send_invoice(
        chat_id=message.chat.id,
        title=f"Подписка CeaAI — {plan_name}",
        description=description,
        payload=str(payment["external_id"]),
        provider_token="",
        currency="XTR",
        prices=[
            LabeledPrice(label=f"{plan_name}: {coins_label}", amount=stars_amount)
        ],
    )


def _format_models(models: list[Dict[str, Any]]) -> str:
    lines = []
    for model in models:
        config = loads_dict(model.get("config"))
        description = str(config.get("ui_description") or "").strip()
        lines.extend(
            [
                f"🤖 {model['display_name']}",
                f"Стоимость: {format_coin_amount(model['coins_cost'])} за запрос.",
            ]
        )
        if description:
            lines.append(description)
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _format_direct_prompt_screen(model: Dict[str, Any]) -> str:
    if str(model["generation_type"]) == "image":
        config = loads_dict(model.get("config"))
        four_k_cost = int(config.get("four_k_coins_cost") or 3)
        return (
            f"Модель: {model['display_name']}\n\n"
            f"Стоимость 1 запроса: {_format_coin_unit(model['coins_cost'])}\n"
            f"Стоимость 1 запроса 4К: {_format_coin_unit(four_k_cost)}\n\n"
            "Введите текст для генерации или изображение которое хотите изменить.\n\n"
            "🔎Чтобы получить изображение 4К, добавьте «4К» в текст запроса"
        )
    prompt_copy = {
        "video": "Опишите видео, которое хотите получить.",
        "tts": "Отправьте текст для озвучки.",
    }.get(str(model["generation_type"]), "Отправьте prompt для выбранной модели.")
    return (
        f"{model['display_name']}\n\n"
        f"Стоимость: {format_coin_amount(model['coins_cost'])} за запрос.\n\n"
        f"{prompt_copy}"
    )


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
            f"💡{model['display_name']}",
            "",
            f"Стоимость 1 запроса: {_format_coin_unit(model['coins_cost'])}",
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
            f"💡{model['display_name']}",
            "",
            f"Стоимость 1 запроса: {_format_coin_unit(model['coins_cost'])}",
            f"Чат «{current_chat['title']}» выбран.",
            "",
            "Введите текст, что хотите спросить у нейросети.",
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


def _format_image_generation_caption(
    *,
    prompt_text: str,
    model: Dict[str, Any],
    coins_charged: Any,
    balance_after: Any,
) -> str:
    return (
        f"📍 Ваш запрос: {prompt_text.strip() or '—'}\n\n"
        f"🎛️ Инструмент: {model['display_name']}\n\n"
        "ℹ️ Списано: "
        f"{_format_coin_unit(coins_charged)}  "
        f"Баланс: {_format_coin_balance_unit(balance_after)}"
    )


def _telegram_caption(text: str, *, limit: int = 1024) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


async def _show_generation_result(
    message: Message,
    services: AppServices,
    user_id: int,
    result: Dict[str, Any],
    *,
    reply_markup: Any | None = None,
    image_caption: str | None = None,
) -> Message:
    if result.get("kind") == "image" and result.get("image_b64"):
        state, payload = _session_state_payload(services, user_id)
        tracked_ids = _tracked_message_ids(payload)
        if tracked_ids:
            await _delete_screen_messages(message, tracked_ids)
        try:
            image_bytes = base64.b64decode(str(result["image_b64"]), validate=True)
            sent = await message.bot.send_photo(
                chat_id=message.chat.id,
                photo=BufferedInputFile(
                    image_bytes,
                    filename=str(result.get("file_name") or "cea-ai.png"),
                ),
                caption=_telegram_caption(
                    str(image_caption or result.get("caption") or "Готово.")
                ),
                reply_markup=None,
            )
            payload.pop(LAST_BOT_MESSAGE_ID, None)
            payload.pop(LAST_BOT_MESSAGE_IDS, None)
            services.users.set_session(user_id, state=state, payload=payload)
            return sent
        except (binascii.Error, TelegramBadRequest, TelegramForbiddenError, ValueError):
            pass

    return await _show_screen(
        message,
        services,
        user_id,
        _format_generation_result(result),
        reply_markup=reply_markup,
    )


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
            f"{format_coin_amount(row['coins_charged'])} — {prompt}"
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
        f"Баланс активных подписок: {format_coin_amount(stats['active_balance_total'])}"
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
        balance_text = (
            format_coin_amount(balance) if balance is not None else format_coin_amount(0)
        )
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
            f"{format_coin_amount(subscription['coins_balance_cache'])}"
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
        f"Потрачено: {format_coin_amount(generations.get('spent_coins', 0))}"
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
    referral_stats = services.referrals.stats(user_id)
    text = _format_menu(
        profile_user,
        subscription,
        invited_users_count=referral_stats.invited_count,
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
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    delete_current: bool = False,
    notice: str | None = None,
) -> None:
    text = "🛠 Админка CeaAI\nВыберите раздел."
    if notice:
        text = f"{notice}\n\n{text}"
    await _show_screen(
        message,
        services,
        user_id,
        text,
        reply_markup=admin_menu_keyboard(
            maintenance_active=services.admin.is_maintenance_mode_active()
        ),
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
    user = services.users.get_by_id(user_id)
    if user and services.admin.is_maintenance_mode_active() and not services.admin.has_admin_access(user):
        await _show_screen(
            message,
            services,
            user_id,
            "❌ Сейчас ведутся технические работы.\n\nПожалуйста, попробуйте позже.",
            reply_markup=None,
            delete_current=delete_current,
        )
        return
    await _show_screen(
        message,
        services,
        user_id,
        "Ваш аккаунт заблокирован. Обратитесь в поддержку.",
        reply_markup=main_menu_keyboard(),
        delete_current=delete_current,
    )


def _is_blocked_regular_user(services: AppServices, user: Dict[str, Any]) -> bool:
    return services.admin.is_restricted_regular_user(user)


def _record_message(handler: str, message: Message) -> None:
    record_message(handler=handler, message=message)


def _format_referral_join_notice(referred_telegram_id: int) -> str:
    return (
        "По вашей партнёрской ссылке пришел новый пользователь 🔥\n\n"
        f"ℹ️ ID: {referred_telegram_id}"
    )


def _format_referral_already_registered_notice() -> str:
    return (
        "❌ Вы уже зарегистрированы в Cea AI.\n\n"
        "Партнёрская ссылка действует только для новых пользователей."
    )


async def _send_referral_join_notice(
    message: Message,
    referral_result: ReferralApplyResult,
) -> None:
    if (
        not referral_result.assigned
        or not referral_result.referrer_telegram_id
        or not referral_result.referred_telegram_id
    ):
        return
    try:
        await message.bot.send_message(
            chat_id=referral_result.referrer_telegram_id,
            text=_format_referral_join_notice(referral_result.referred_telegram_id),
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


async def _send_referral_already_registered_notice(message: Message) -> None:
    try:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=_format_referral_already_registered_notice(),
            reply_markup=main_menu_keyboard(),
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


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
            f"Баланс: {format_coin_amount(subscription['coins_balance_cache'])}\n"
            f"Подписка: {subscription['plan_name']} до {subscription['ends_at'][:10]}"
        )
    else:
        text = "Активной подписки нет. Выберите тариф и оформите подписку."
    await _show_screen(
        message,
        services,
        user_id,
        text,
        reply_markup=(
            main_menu_keyboard() if subscription else subscription_required_keyboard()
        ),
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
    subscription = services.subscriptions.active_for_user(user_id)
    await _show_screen(
        message,
        services,
        user_id,
        _format_plans(plans),
        reply_markup=plans_keyboard(
            plans,
            has_active_subscription=subscription is not None,
        ),
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
    skip_single_model_choice: bool = False,
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
    if skip_single_model_choice and len(models) == 1:
        model = models[0]
        _set_dialog_state(
            services,
            user_id,
            state="waiting_prompt",
            payload={"model_price_id": int(model["id"])},
        )
        await _show_screen(
            message,
            services,
            user_id,
            _format_direct_prompt_screen(model),
            reply_markup=back_to_menu_keyboard(),
            delete_current=delete_current,
        )
        return
    _set_dialog_state(
        services,
        user_id,
        state="waiting_model_choice",
        payload=_model_choice_payload(models),
    )
    screen_text = (
        title
        if generation_types == {"text"}
        else f"{title}\n\n{_format_models(models)}"
    )
    await _show_screen(
        message,
        services,
        user_id,
        screen_text,
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
                reply_markup=inline_back_to_menu_keyboard(),
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
                reply_markup=inline_back_to_menu_keyboard(),
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
                _format_direct_prompt_screen(model),
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
        "нейросети: chatgpt, deepseek",
        "нейросети: gpt, deepseek",
        "нейросети chatgpt deepseek",
        "нейросети gpt deepseek",
    } or text == TEXT_AI_BUTTON:
        await _send_models_for_types(
            message,
            services,
            user["id"],
            generation_types={"text"},
            title="💡Выберите текстовую модель:",
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
            skip_single_model_choice=True,
            delete_current=True,
        )
        return True

    if text_lower == "видео с ai" or text == VIDEO_AI_BUTTON:
        await _show_screen(
            message,
            services,
            user["id"],
            _feature_temporarily_unavailable_message("Видео с AI"),
            reply_markup=back_to_menu_keyboard(),
            delete_current=True,
        )
        return True

    if text_lower in {"озвучка с ai", "озвучка текста"} or text == VOICE_AI_BUTTON:
        await _show_screen(
            message,
            services,
            user["id"],
            _feature_temporarily_unavailable_message("Озвучка с AI"),
            reply_markup=back_to_menu_keyboard(),
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
        existing_user = services.users.get_by_telegram_id(message.from_user.id)
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        if _is_blocked_regular_user(services, user):
            await _send_blocked_notice(message, services, user["id"])
            return
        referral_result = services.referrals.apply_start_referral(
            user_id=user["id"],
            start_text=message.text,
            user_was_registered=existing_user is not None,
        )
        if referral_result.already_registered:
            _clear_dialog_state(services, user["id"])
            await _send_referral_already_registered_notice(message)
            return
        await _send_referral_join_notice(message, referral_result)
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

        _track_existing_screen_message(services, user["id"], callback.message)
        parts = callback.data.split(":")
        action = parts[1] if len(parts) > 1 else "home"
        try:
            if action == "home":
                _clear_dialog_state(services, user["id"])
                await _send_admin_home(callback.message, services, user["id"])
            elif action == "stats":
                stats = services.admin.stats()
                await _show_screen(
                    callback.message,
                    services,
                    user["id"],
                    _format_admin_stats(stats),
                    reply_markup=admin_back_keyboard(),
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
                    "Введите положительное целое число коинов для начисления.",
                    reply_markup=admin_back_keyboard(),
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
                )
            elif action == "maintenance":
                is_active = services.admin.toggle_maintenance_mode(admin=admin)
                await _send_admin_home(
                    callback.message,
                    services,
                    user["id"],
                    notice=(
                        "❌ Тех работы включены."
                        if is_active
                        else "✅ Тех работы выключены."
                    ),
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

    @router.callback_query(F.data == "menu:subscription")
    async def menu_subscription(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        if callback.message:
            await _send_main_menu(
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

    @router.callback_query(F.data == "subscription:cancel_auto_renew")
    async def cancel_auto_renew(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return

        subscription = services.subscriptions.active_for_user(user["id"])
        if subscription is None:
            notice = "ℹ️ У вас нет активной подписки для отключения автопродления."
        elif not subscription.get("auto_renew"):
            notice = "ℹ️ Автопродление уже выключено."
        else:
            services.subscriptions.disable_auto_renew(user["id"])
            notice = "✅ Автопродление отключено. Повторных списаний не будет."

        if callback.message:
            await _send_main_menu(
                callback.message,
                services,
                user["id"],
                intro=notice,
                delete_current=True,
            )
        await callback.answer()

    @router.callback_query(F.data == "subscription:cancel_placeholder")
    async def cancel_subscription_placeholder(callback: CallbackQuery) -> None:
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
                "❌ Отмена подписки пока не подключена.\n\n"
                "Если нужно остановить будущие списания, используйте кнопку "
                "«Отключить автопродление» в профиле.",
                reply_markup=back_to_menu_keyboard(),
                delete_current=True,
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
        plan = next(
            (
                candidate
                for candidate in services.catalog.list_plans()
                if candidate["code"] == plan_code
            ),
            None,
        )
        if plan is None:
            if callback.message:
                await _show_screen(
                    callback.message,
                    services,
                    user["id"],
                    "Тариф не найден",
                    reply_markup=inline_back_to_menu_keyboard(),
                    delete_current=True,
                )
            await callback.answer()
            return

        _set_dialog_state(
            services,
            user["id"],
            state="waiting_payment_method",
            payload={"plan_code": plan_code},
        )
        if callback.message:
            await _show_screen(
                callback.message,
                services,
                user["id"],
                _format_plan_details(plan),
                reply_markup=payment_methods_keyboard(plan_code),
                delete_current=True,
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("pay_method:"))
    async def choose_payment_method(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        parts = callback.data.split(":", 2) if callback.data else []
        plan_code = parts[1] if len(parts) >= 2 else ""
        payment_method = parts[2] if len(parts) >= 3 else ""

        try:
            payment = await asyncio.to_thread(
                services.payments.create_payment,
                user_id=user["id"],
                plan_code=plan_code,
                payment_method=payment_method,
            )
        except (BusinessRuleError, NotFoundError) as exc:
            record_error(exception=exc, update=callback)
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

        if payment["provider"] == "telegram_stars":
            if callback.message:
                invoice_message = await _send_telegram_stars_invoice(
                    callback.message, payment
                )
                _set_dialog_state(
                    services,
                    user["id"],
                    state="waiting_payment",
                    payload={
                        "payment_id": payment["id"],
                        "payment_method": payment_method,
                        TELEGRAM_STARS_INVOICE_MESSAGE_ID: (
                            invoice_message.message_id
                        ),
                    },
                )
                await _show_screen(
                    callback.message,
                    services,
                    user["id"],
                    f"Способ оплаты: {_payment_method_label(payment_method)}\n\n"
                    "Счёт Telegram Stars отправлен отдельным сообщением. "
                    "Откройте его и подтвердите оплату.",
                    reply_markup=back_to_menu_keyboard(),
                )
            await callback.answer()
            return

        _set_dialog_state(
            services,
            user["id"],
            state="waiting_payment",
            payload={"payment_id": payment["id"], "payment_method": payment_method},
        )
        selected_plan = next(
            (
                candidate
                for candidate in services.catalog.list_plans()
                if str(candidate.get("code")) == plan_code
            ),
            None,
        )
        if payment["provider"] == "mock":
            payment_text = (
                f"Способ оплаты: {_payment_method_label(payment_method)}\n\n"
                "Платёж создан со статусом pending.\n"
                "Нажмите кнопку ниже, чтобы подтвердить оплату."
            )
        elif payment["provider"] == "yookassa" and selected_plan is not None:
            payment_text = _format_yookassa_payment_screen(
                selected_plan,
                public_offer_url=services.settings.public_offer_url,
            )
        else:
            payment_text = (
                f"Способ оплаты: {_payment_method_label(payment_method)}\n\n"
                "Платёж создан. Нажмите кнопку ниже и завершите оплату.\n"
                "Коины начислятся автоматически после подтверждения платежа."
            )
        if callback.message:
            await _show_screen(
                callback.message,
                services,
                user["id"],
                payment_text,
                reply_markup=payment_keyboard(
                    payment["id"],
                    payment["payment_url"],
                    provider=str(payment["provider"]),
                ),
                delete_current=True,
            )
        await callback.answer()

    @router.callback_query(F.data == "coins:buy")
    async def buy_coins_placeholder(callback: CallbackQuery) -> None:
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
                _format_crystal_packages(),
                reply_markup=crystal_packages_keyboard(),
                delete_current=True,
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("crystals:"))
    async def buy_crystals_placeholder(callback: CallbackQuery) -> None:
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
                "Покупка коинов скоро будет доступна.",
                reply_markup=crystal_packages_keyboard(),
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
                f"Начислено: {format_coin_amount(result.credited_coins)}.\n"
                "Текущий баланс: "
                f"{format_coin_amount(result.subscription['coins_balance_cache'])}."
            )
        else:
            text = "Этот платёж уже был обработан. Повторного начисления нет."
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

    @router.callback_query(F.data.startswith("models:type:"))
    async def menu_models_by_type(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        if not callback.message or not callback.data:
            await callback.answer()
            return
        generation_type = callback.data.rsplit(":", 1)[-1]
        config = {
            "text": ("💡Выберите текстовую модель:", False),
            "image": ("Выберите модель для фото с AI.", True),
            "video": ("Выберите модель для видео с AI.", False),
            "tts": ("Выберите модель для озвучки текста.", False),
        }.get(generation_type)
        if config is None:
            await callback.answer("Неизвестный раздел", show_alert=True)
            return
        if generation_type in {"video", "tts"}:
            feature_name = "Видео с AI" if generation_type == "video" else "Озвучка с AI"
            _clear_dialog_state(services, user["id"])
            await _show_screen(
                callback.message,
                services,
                user["id"],
                _feature_temporarily_unavailable_message(feature_name),
                reply_markup=back_to_menu_keyboard(),
            )
            await callback.answer()
            return
        title, skip_single_model_choice = config
        _clear_dialog_state(services, user["id"])
        await _send_models_for_types(
            callback.message,
            services,
            user["id"],
            generation_types={generation_type},
            title=title,
            skip_single_model_choice=skip_single_model_choice,
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
                _format_direct_prompt_screen(model),
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
                reply_markup=inline_back_to_menu_keyboard(),
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
            referral_stats = services.referrals.stats(user["id"])
            await _show_screen(
                callback.message,
                services,
                user["id"],
                _format_referral_screen(
                    user,
                    invited_users_count=referral_stats.invited_count,
                    balance_kopecks=referral_stats.balance_kopecks,
                    withdrawal_method=referral_stats.withdrawal_method,
                    requisites=referral_stats.requisites,
                    rate_percent=referral_stats.rate_percent,
                    withdrawal_min_kopecks=referral_stats.withdrawal_min_kopecks,
                ),
                reply_markup=referral_keyboard(),
                delete_current=True,
                parse_mode="HTML",
            )
        await callback.answer()

    @router.callback_query(F.data == "referral:withdraw")
    async def referral_withdraw(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        if not callback.message:
            await callback.answer()
            return

        referral_stats = services.referrals.stats(user["id"])
        if referral_stats.balance_kopecks < referral_stats.withdrawal_min_kopecks:
            await _show_screen(
                callback.message,
                services,
                user["id"],
                _format_referral_withdrawal_unavailable(
                    referral_stats.withdrawal_min_kopecks
                ),
                reply_markup=inline_back_to_menu_keyboard(),
                parse_mode="HTML",
            )
            await callback.answer()
            return

        await _show_screen(
            callback.message,
            services,
            user["id"],
            _format_referral_withdrawal_available(
                support_username=services.settings.support_username,
                balance_kopecks=referral_stats.balance_kopecks,
            ),
            reply_markup=inline_back_to_menu_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()

    @router.pre_checkout_query()
    async def telegram_stars_pre_checkout(pre_checkout: PreCheckoutQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(pre_checkout))
        if _is_blocked_regular_user(services, user):
            await pre_checkout.answer(
                ok=False,
                error_message="Ваш аккаунт заблокирован. Обратитесь в поддержку.",
            )
            return
        try:
            services.payments.validate_telegram_stars_pre_checkout(
                invoice_payload=pre_checkout.invoice_payload,
                currency=pre_checkout.currency,
                total_amount=pre_checkout.total_amount,
            )
        except (BusinessRuleError, NotFoundError) as exc:
            await pre_checkout.answer(ok=False, error_message=str(exc))
            return
        await pre_checkout.answer(ok=True)

    @router.message(F.successful_payment)
    async def telegram_stars_successful_payment(message: Message) -> None:
        _record_message("telegram_stars_successful_payment", message)
        await _delete_user_message(message)
        user = services.users.ensure_telegram_user(**_user_kwargs(message))
        await _delete_telegram_stars_invoice_message(message, services, user["id"])
        payment = message.successful_payment
        if payment is None:
            return
        try:
            result = services.payments.process_telegram_stars_successful_payment(
                invoice_payload=payment.invoice_payload,
                currency=payment.currency,
                total_amount=payment.total_amount,
                telegram_payment_charge_id=payment.telegram_payment_charge_id,
                provider_payment_charge_id=payment.provider_payment_charge_id,
            )
        except (BusinessRuleError, NotFoundError) as exc:
            await _show_screen(
                message,
                services,
                user["id"],
                str(exc),
                reply_markup=back_to_menu_keyboard(),
                delete_current=True,
            )
            return

        _clear_dialog_state(services, user["id"])
        if result.processed and result.subscription:
            text = (
                "Оплата Telegram Stars прошла успешно.\n"
                f"Начислено {format_coin_amount(result.credited_coins)}.\n"
                "Текущий баланс: "
                f"{format_coin_amount(result.subscription['coins_balance_cache'])}."
            )
        else:
            text = "Этот платёж уже был обработан. Повторного начисления нет."
        await _show_screen(
            message,
            services,
            user["id"],
            text,
            reply_markup=main_menu_button_keyboard(),
            delete_current=True,
        )

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
                    f"Начислено {format_coin_amount(amount)}. "
                    f"Новый баланс: {format_coin_amount(balance)}.\n\n"
                    f"{_format_admin_user_card(card)}",
                    reply_markup=admin_user_card_keyboard(
                        card, can_manage=services.admin.can_manage(admin)
                    ),
                )
            except ValueError:
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    "Введите положительное целое число.",
                    reply_markup=admin_back_keyboard(),
                )
            except (BusinessRuleError, NotFoundError) as exc:
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    str(exc),
                    reply_markup=admin_back_keyboard(),
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
                notice="Выберите чат кнопкой в сообщении.",
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
                reply_markup=None,
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
                    _subscription_required_message(),
                    reply_markup=subscription_required_keyboard(),
                )
                return
            except InsufficientCoinsError:
                _clear_dialog_state(services, user["id"])
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    "Недостаточно коинов для этой модели. Выберите тариф или модель дешевле.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            except GenerationProviderFailedError as exc:
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    str(exc),
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

            await _show_generation_result(
                message,
                services,
                user["id"],
                generation.result,
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
                "Выберите модель кнопкой в сообщении.",
                reply_markup=models_keyboard(models) if models else main_menu_keyboard(),
                delete_current=True,
            )
            return

        if not session or session["state"] != "waiting_prompt":
            await _show_screen(
                message,
                services,
                user["id"],
                "Выберите действие кнопкой в сообщении.",
                reply_markup=main_menu_keyboard(),
                delete_current=True,
            )
            return

        payload = loads_dict(session.get("payload"))
        model_price_id = int(payload.get("model_price_id", 0))
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

        image_input: ImageInput | None = None
        if model["generation_type"] == "image":
            try:
                image_input = await _image_input_from_message(message)
            except ValueError as exc:
                await _show_screen(
                    message,
                    services,
                    user["id"],
                    str(exc),
                    reply_markup=back_to_menu_keyboard(),
                    delete_current=True,
                )
                return

        prompt_text = (message.text or message.caption or "").strip()
        if image_input is not None and not prompt_text:
            prompt_text = DEFAULT_IMAGE_EDIT_PROMPT
        if not prompt_text:
            prompt_hint = (
                "Введите текст для генерации или отправьте изображение с описанием правки."
                if model["generation_type"] == "image"
                else "Отправьте текстовый prompt."
            )
            await _show_screen(
                message,
                services,
                user["id"],
                prompt_hint,
                reply_markup=back_to_menu_keyboard(),
                delete_current=True,
            )
            return

        await _show_screen(
            message,
            services,
            user["id"],
            "Запускаю генерацию...",
            reply_markup=None,
            delete_current=True,
        )
        try:
            generation = services.generations.generate(
                user_id=user["id"],
                model_price_id=model_price_id,
                prompt_text=prompt_text,
                image_input=image_input,
            )
        except NoActiveSubscriptionError:
            _clear_dialog_state(services, user["id"])
            await _show_screen(
                message,
                services,
                user["id"],
                _subscription_required_message(),
                reply_markup=subscription_required_keyboard(),
            )
            return
        except InsufficientCoinsError:
            _clear_dialog_state(services, user["id"])
            await _show_screen(
                message,
                services,
                user["id"],
                "Недостаточно коинов для этой модели. Выберите тариф или модель дешевле.",
                reply_markup=back_to_menu_keyboard(),
            )
            return
        except GenerationProviderFailedError as exc:
            _clear_dialog_state(services, user["id"])
            await _show_screen(
                message,
                services,
                user["id"],
                str(exc),
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

        await _show_generation_result(
            message,
            services,
            user["id"],
            generation.result,
            reply_markup=(
                None
                if generation.model["generation_type"] == "image"
                else back_to_menu_keyboard()
            ),
            image_caption=(
                _format_image_generation_caption(
                    prompt_text=prompt_text,
                    model=generation.model,
                    coins_charged=generation.generation["coins_charged"],
                    balance_after=generation.balance_after,
                )
                if generation.model["generation_type"] == "image"
                else None
            ),
        )

    return router
