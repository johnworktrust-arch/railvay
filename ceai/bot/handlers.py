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
    InputMediaAudio,
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
    GIFT_BUTTON,
    HELP_BUTTON,
    HISTORY_BUTTON,
    PHOTO_AI_BUTTON,
    PROFILE_BUTTON,
    REFERRAL_BUTTON,
    REPLY_MENU_BUTTONS,
    START_WORK_BUTTON,
    TEXT_AI_BUTTON,
    VIDEO_AI_BUTTON,
    VOICE_AI_BUTTON,
    admin_back_keyboard,
    admin_menu_keyboard,
    admin_user_card_keyboard,
    admin_users_keyboard,
    about_service_keyboard,
    back_to_menu_keyboard,
    crystal_packages_keyboard,
    gift_subscription_keyboard,
    history_keyboard,
    history_result_keyboard,
    inline_back_to_menu_keyboard,
    main_menu_button_keyboard,
    main_menu_keyboard,
    model_choice_label,
    models_keyboard,
    payment_keyboard,
    payment_methods_keyboard,
    plans_keyboard,
    profile_keyboard,
    referral_keyboard,
    subscription_required_keyboard,
    text_chat_keyboard,
    text_chat_label,
    text_chat_prompt_keyboard,
    tts_voice_keyboard,
    work_access_required_keyboard,
    work_menu_keyboard,
)
from ceai.config import DEFAULT_PRIVACY_POLICY_URL, DEFAULT_PUBLIC_OFFER_URL
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
LAST_SCREEN_HAS_MEDIA = "last_screen_has_media"
TELEGRAM_STARS_INVOICE_MESSAGE_ID = "telegram_stars_invoice_message_id"
START_TEXT_ALIASES = {"старт", "/старт", "start", "/start", "начать"}
TTS_VOICE_SAMPLES_DIR = Path(__file__).resolve().parents[1] / "assets" / "tts_voices"
MAX_IMAGE_INPUT_BYTES = 20 * 1024 * 1024
DEFAULT_IMAGE_EDIT_PROMPT = "Улучши изображение, сохранив основной сюжет."
HISTORY_PAGE_SIZE = 3
GIFT_CHANNEL_USERNAME = "ceafamily"
GIFT_CHANNEL_CHAT_ID = f"@{GIFT_CHANNEL_USERNAME}"
GIFT_CHANNEL_URL = f"https://t.me/{GIFT_CHANNEL_USERNAME}"
GIFT_DURATION_DAYS = 3
GIFT_COINS_AMOUNT = 5
GIFT_PLAN_CODE = "start"
TTS_VOICES = (
    ("Alloy", "alloy"),
    ("Echo", "echo"),
    ("Fable", "fable"),
    ("Onyx", "onyx"),
    ("Nova", "nova"),
    ("Shimmer", "shimmer"),
)


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
    is_media: bool = False,
) -> None:
    payload.pop(LAST_BOT_MESSAGE_ID, None)
    payload[LAST_BOT_MESSAGE_IDS] = [message_id]
    if is_media:
        payload[LAST_SCREEN_HAS_MEDIA] = True
    else:
        payload.pop(LAST_SCREEN_HAS_MEDIA, None)
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


async def _send_generation_menu_followup(
    message: Message,
    services: AppServices,
    user_id: int,
) -> Message:
    _reset_dialog_state(services, user_id)
    state, payload = _session_state_payload(services, user_id)
    reply_markup = work_menu_keyboard()
    await _remove_legacy_reply_keyboard(message, payload, reply_markup)
    sent = await _send_screen_message(
        message,
        text=_format_work_menu(),
        reply_markup=reply_markup,
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


async def _send_main_menu_followup(
    message: Message,
    services: AppServices,
    user_id: int,
) -> Message:
    _reset_dialog_state(services, user_id)
    reply_markup = _main_menu_keyboard(services, user_id)
    sent = await _send_screen_message(
        message,
        text=_format_main_menu(),
        reply_markup=reply_markup,
    )
    _remember_screen_message(
        services,
        user_id,
        state="idle",
        payload={},
        message_id=sent.message_id,
        reply_markup=reply_markup,
    )
    return sent


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
    last_screen_has_media = bool(payload.get(LAST_SCREEN_HAS_MEDIA))
    current_bot_message_id = (
        message.message_id
        if message.message_id and not _is_user_message(message)
        else None
    )
    last_message_id = (
        current_bot_message_id
        if current_bot_message_id is not None
        else tracked_ids[-1] if tracked_ids else None
    )
    await _remove_legacy_reply_keyboard(message, payload, reply_markup)
    replace_current = isinstance(
        reply_markup, (ReplyKeyboardMarkup, ReplyKeyboardRemove)
    ) or (delete_current and _is_user_message(message)) or last_screen_has_media

    # Bottom-keyboard actions arrive as user messages, so they should replace
    # the previous bot screen. Inline callback actions edit the message that
    # owns the pressed button, even if an old session points at another screen.
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
) -> str:
    if subscription:
        balance = subscription["coins_balance_cache"]
        plan = subscription["plan_name"]
        sub_line = f"⭐ Подписка: {escape(str(plan))}"
        expires_line = (
            "📅 Срок действия: "
            f"{escape(format_datetime_russian_minute(subscription.get('ends_at')))}"
        )
    else:
        balance = 0
        sub_line = "⭐ Подписка: нет активной"
        expires_line = "📅 Срок действия: —"
    return (
        f"👤 Профиль: {_profile_link(user)}\n\n"
        f"ℹ️ ID: {user.get('telegram_id') or user.get('id')}\n"
        f"💰 Баланс: {format_coin_amount(balance)}\n"
        f"{sub_line}\n"
        f"{expires_line}"
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
    rate_percent: int = REFERRAL_RATE_PERCENT,
    withdrawal_min_kopecks: int = REFERRAL_WITHDRAWAL_MIN_KOPECKS,
) -> str:
    withdrawal_min_text = format_rubles_from_kopecks(withdrawal_min_kopecks).replace(
        " ₽", "₽"
    )
    return (
        "👥 <b>Приглашайте друзей и зарабатывайте "
        f"{rate_percent}% с каждого пополнения!</b>\n\n"
        "Например:\n"
        "<blockquote>"
        "— Друзья перешли по вашей ссылке и потратили 1000₽\n"
        "— Вы получаете 300₽!"
        "</blockquote>\n\n"
        "📊 <b>Ваша статистика:</b>\n"
        "<blockquote>"
        f"— Приглашено: {invited_users_count}\n"
        f"— Баланс: {escape(format_rubles_from_kopecks(balance_kopecks))}"
        "</blockquote>\n\n"
        f"% <b>Текущая ставка: {rate_percent}%</b>\n"
        f"💼 Вывод доступен от {escape(withdrawal_min_text)}\n\n"
        "🔗 <b>Пригласительная ссылка:</b>\n"
        f"<code>{escape(_referral_link(user))}</code>"
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


def _format_main_menu() -> str:
    return (
        "👋 Привет! Я Cea AI — бот с современными нейросетями для текста, "
        "изображений, видео и озвучки.\n\n"
        "🏠 Главное меню\n"
        "Выберите нужный раздел 👇"
    )


def _format_work_menu() -> str:
    return (
        "🔥 Начать работу с AI-инструментами\n\n"
        "Выберите, что хотите сделать прямо сейчас👇"
    )


def _format_gift_screen() -> str:
    return (
        f"🎁 <b>{format_coin_amount(GIFT_COINS_AMOUNT)} бесплатно</b>\n\n"
        f"Чтобы получить доступ, подпишитесь на канал "
        f"@{GIFT_CHANNEL_USERNAME}.\n\n"
        "<blockquote>▶ После подписки нажмите проверку ❞</blockquote>"
    )


def _format_gift_not_subscribed() -> str:
    return (
        "❌ Подписка на канал не найдена.\n\n"
        f"Подпишитесь на @{GIFT_CHANNEL_USERNAME} и нажмите "
        "«Проверить подписку» ещё раз."
    )


def _format_gift_check_unavailable() -> str:
    return (
        "❌ Не удалось проверить подписку на канал.\n\n"
        f"Проверьте, что бот добавлен администратором в @{GIFT_CHANNEL_USERNAME}, "
        "или попробуйте ещё раз позже."
    )


def _format_gift_activated(result: Dict[str, Any]) -> str:
    return (
        f"🎁 <b>Пробный доступ активирован на {GIFT_DURATION_DAYS} дня</b>\n\n"
        "На ваш баланс зачислено "
        f"{int(result.get('credited_coins') or 0)} Coin."
    )


def _format_gift_already_claimed(result: Dict[str, Any]) -> str:
    subscription = result.get("subscription") or {}
    balance = subscription.get("coins_balance_cache", 0)
    return (
        "✅ Подписка на канал подтверждена.\n\n"
        "Подарок уже был активирован на этом аккаунте, повторное начисление "
        "недоступно.\n"
        f"Текущий баланс: {format_coin_amount(balance)}"
    )


def _format_plans(plans: list[Dict[str, Any]]) -> str:
    return (
        "💳 Тарифы Cea AI\n\n"
        "Каждый тариф действует 30 дней и открывает доступ ко всем "
        "AI-инструментам.\n\n"
        "Выберите тариф ниже, чтобы посмотреть подробности 👇"
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
    image_requests = coins // 3
    video_requests = coins // 25
    tts_requests = coins // 3
    stars_amount = telegram_stars_amount_for_rub(price)
    features = loads_dict(plan.get("features"))
    description = str(features.get("description") or "").strip()
    usage_example = str(features.get("usage_example") or "").strip()
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
    lines = [
        f"{meta['icon']} {meta['label']} — {price} ₽ / {stars_amount} ⭐",
        f"({meta['tag']})",
        "",
    ]
    if description:
        lines.extend([description, ""])
    lines.extend(
        [
            "➕ DeepSeek, ChatGPT, GPT Image, Kling и озвучка",
            f"➕ {format_coin_amount(coins)}",
            f"➕ До {deepseek_requests} запросов DeepSeek",
            f"➕ До {chatgpt_requests} запросов ChatGPT",
            f"➕ До {image_requests} изображений GPT Image",
            f"➕ До {video_requests} видео Kling",
            f"➕ До {tts_requests} озвучек",
            "➕ Срок действия — 30 дней",
        ]
    )
    if usage_example:
        lines.extend(["", f"Пример использования: {usage_example}."])
    lines.extend([str(meta["extra"]), "", _format_payment_methods()])
    return "\n".join(lines)


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
        "обработки данных и пользовательским соглашением.\n\n"
        f"Пользовательское соглашение: {offer_url}\n\n"
        "Если потребуется отключить автоматическое продление, напишите в поддержку."
    )


def _format_platega_payment_screen(
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
        "Проверка платежа происходит автоматически. "
        "Коины начислятся сразу после подтверждения оплаты.\n\n"
        "Нажимая «Оплатить», вы подтверждаете согласие с условиями "
        "обработки данных и пользовательским соглашением.\n\n"
        f"Пользовательское соглашение: {offer_url}"
    )


def _subscription_required_message() -> str:
    return "Нужна активная подписка. Откройте тарифы и выберите подписку."


def _work_access_required_message() -> str:
    return (
        "❌ У вас нет активной подписки.\n\n"
        "Активируйте бесплатный доступ или выберите подходящий тариф."
    )


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
        return (
            f"Модель: {model['display_name']}\n\n"
            f"Стоимость 1 запроса: {_format_coin_unit(model['coins_cost'])}\n\n"
            "Введите текст для генерации или изображение, которое хотите изменить."
        )
    if str(model["generation_type"]) == "video":
        return (
            f"Модель: {model['display_name']}\n\n"
            f"Стоимость 1 запроса: {_format_coin_unit(model['coins_cost'])}\n\n"
            "Введите текст для генерации видео."
        )
    prompt_copy = {
        "tts": "Отправьте текст для озвучки.",
    }.get(str(model["generation_type"]), "Отправьте prompt для выбранной модели.")
    return (
        f"{model['display_name']}\n\n"
        f"Стоимость: {format_coin_amount(model['coins_cost'])} за запрос.\n\n"
        f"{prompt_copy}"
    )


def _format_tts_prompt_screen(model: Dict[str, Any], *, voice: str) -> str:
    voice_label = next(
        label for label, voice_key in TTS_VOICES if voice_key == voice
    )
    return (
        f"🎙 {model['display_name']}\n\n"
        f"Голос: {voice_label}\n"
        f"Стоимость: {format_coin_amount(model['coins_cost'])} за запрос.\n\n"
        "Отправьте текст для озвучки."
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
        url = str(result.get("url") or "")
        caption = str(result.get("caption") or "Готово.")
        if url.strip():
            caption = escape(caption)
        link = _format_media_link(kind=str(kind), url=url)
        body = "\n".join(part for part in (caption, link) if part)
    elif kind == "tts":
        body = f"{result.get('message', 'Mock TTS result')}\n{result.get('url')}"
    else:
        body = str(result)
    return body


def _format_media_link(*, kind: str, url: str) -> str:
    cleaned_url = url.strip()
    if not cleaned_url:
        return ""
    label = "ссылка на видео" if kind == "video" else "ссылка на фото"
    prefix = "🎬 Видео" if kind == "video" else "🖼 Фото"
    return f'{prefix}: <a href="{escape(cleaned_url, quote=True)}">{label}</a>'


def _result_uses_html_links(result: Dict[str, Any]) -> bool:
    return str(result.get("kind") or "") in {"image", "video"} and bool(
        str(result.get("url") or "").strip()
    )


def _caption_without_media_link(text: str) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not line.startswith("🎬 Видео:") and not line.startswith("🖼 Фото:")
    ]
    return "\n".join(lines).strip()


def _format_media_generation_caption(
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


def _format_video_generation_result(
    *,
    prompt_text: str,
    model: Dict[str, Any],
    coins_charged: Any,
    balance_after: Any,
    result: Dict[str, Any],
) -> str:
    url = str(result.get("url") or "").strip()
    lines = [
        f"📍 Ваш запрос: {escape(prompt_text.strip() or '—')}",
        "",
        f"🎛️ Инструмент: {escape(str(model['display_name']))}",
        "",
        "ℹ️ Списано: "
        f"{_format_coin_unit(coins_charged)}  "
        f"Баланс: {_format_coin_balance_unit(balance_after)}",
    ]
    if url:
        lines.extend(["", _format_media_link(kind="video", url=url)])
    return "\n".join(lines)


def _telegram_caption(text: str, *, limit: int = 1024) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _sent_photo_file_id(sent: Message) -> str | None:
    photos = getattr(sent, "photo", None) or []
    if not photos:
        return None
    file_id = getattr(photos[-1], "file_id", None)
    return str(file_id).strip() if file_id else None


def _sent_video_file_id(sent: Message) -> str | None:
    video = getattr(sent, "video", None)
    file_id = getattr(video, "file_id", None) if video else None
    return str(file_id).strip() if file_id else None


def _sent_audio_file_id(sent: Message) -> str | None:
    audio = getattr(sent, "audio", None)
    file_id = getattr(audio, "file_id", None) if audio else None
    return str(file_id).strip() if file_id else None


async def _remember_generation_media_file(
    services: AppServices,
    *,
    generation_id: int | None,
    kind: str,
    file_id: str | None,
) -> None:
    if generation_id is None or not file_id:
        return
    await asyncio.to_thread(
        services.generations.remember_telegram_media_file,
        generation_id=generation_id,
        kind=kind,
        file_id=file_id,
    )


async def _show_generation_result(
    message: Message,
    services: AppServices,
    user_id: int,
    result: Dict[str, Any],
    *,
    reply_markup: Any | None = None,
    image_caption: str | None = None,
    video_caption: str | None = None,
    audio_caption: str | None = None,
    generation_id: int | None = None,
    send_menu_followup: bool = False,
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
            await _remember_generation_media_file(
                services,
                generation_id=generation_id,
                kind="image",
                file_id=_sent_photo_file_id(sent),
            )
            if send_menu_followup:
                await _send_generation_menu_followup(message, services, user_id)
            return sent
        except (binascii.Error, TelegramBadRequest, TelegramForbiddenError, ValueError):
            pass

    if result.get("kind") == "video" and result.get("url"):
        state, payload = _session_state_payload(services, user_id)
        tracked_ids = _tracked_message_ids(payload)
        if tracked_ids:
            await _delete_screen_messages(message, tracked_ids)
        try:
            caption_source = str(video_caption or result.get("caption") or "Готово.")
            if video_caption:
                caption_source = _caption_without_media_link(caption_source)
            sent = await message.bot.send_video(
                chat_id=message.chat.id,
                video=str(result["url"]),
                caption=_telegram_caption(caption_source),
                parse_mode="HTML" if video_caption else None,
                reply_markup=None,
            )
            payload.pop(LAST_BOT_MESSAGE_ID, None)
            payload.pop(LAST_BOT_MESSAGE_IDS, None)
            services.users.set_session(user_id, state=state, payload=payload)
            await _remember_generation_media_file(
                services,
                generation_id=generation_id,
                kind="video",
                file_id=_sent_video_file_id(sent),
            )
            if send_menu_followup:
                await _send_generation_menu_followup(message, services, user_id)
            return sent
        except (TelegramBadRequest, TelegramForbiddenError, ValueError):
            pass

    if result.get("kind") == "tts" and result.get("audio_b64"):
        state, payload = _session_state_payload(services, user_id)
        tracked_ids = _tracked_message_ids(payload)
        if tracked_ids:
            await _delete_screen_messages(message, tracked_ids)
        try:
            audio_bytes = base64.b64decode(str(result["audio_b64"]), validate=True)
            sent = await message.bot.send_audio(
                chat_id=message.chat.id,
                audio=BufferedInputFile(
                    audio_bytes,
                    filename=str(result.get("file_name") or "cea-ai-voice.mp3"),
                ),
                caption=_telegram_caption(
                    str(
                        audio_caption
                        or result.get("message")
                        or "Озвучка готова."
                    )
                ),
                reply_markup=reply_markup,
            )
            await _remember_generation_media_file(
                services,
                generation_id=generation_id,
                kind="tts",
                file_id=_sent_audio_file_id(sent),
            )
            _remember_screen_message(
                services,
                user_id,
                state=state,
                payload=payload,
                message_id=sent.message_id,
                reply_markup=reply_markup,
                is_media=True,
            )
            if send_menu_followup:
                await _send_generation_menu_followup(message, services, user_id)
            return sent
        except (binascii.Error, TelegramBadRequest, TelegramForbiddenError, ValueError):
            pass

    fallback_text = (
        video_caption
        if result.get("kind") == "video" and video_caption
        else _format_generation_result(result)
    )
    shown = await _show_screen(
        message,
        services,
        user_id,
        fallback_text,
        reply_markup=reply_markup,
        parse_mode=(
            "HTML"
            if (video_caption or _result_uses_html_links(result))
            else None
        ),
    )
    if send_menu_followup and result.get("kind") in {"image", "video", "tts"}:
        await _send_generation_menu_followup(message, services, user_id)
    return shown


def _short_history_text(text: str, *, limit: int = 180) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized or "—"
    return normalized[: limit - 3].rstrip() + "..."


def _generation_prompt_text(row: Dict[str, Any]) -> str:
    prompt_payload = row.get("prompt_payload")
    if not isinstance(prompt_payload, dict):
        prompt_payload = loads_dict(row.get("prompt"))
    prompt = prompt_payload.get("text") or prompt_payload.get("prompt")
    return str(prompt or "").strip()


def _generation_result_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    result_payload = row.get("result_payload")
    if not isinstance(result_payload, dict):
        result_payload = loads_dict(row.get("result"))
    return result_payload


def _generation_result_has_media_link(row: Dict[str, Any]) -> bool:
    result_payload = _generation_result_payload(row)
    return str(result_payload.get("kind") or "") in {"image", "video"} and bool(
        str(result_payload.get("url") or "").strip()
    )


def _generation_result_text(row: Dict[str, Any], *, html_links: bool = False) -> str:
    result_payload = _generation_result_payload(row)
    if not result_payload:
        return str(row.get("error_message") or "Результат пока не сохранён.")

    kind = result_payload.get("kind")
    parts: list[str] = []
    if kind == "text":
        parts.append(str(result_payload.get("text") or "").strip())
    elif kind in {"image", "video"}:
        caption = str(result_payload.get("caption") or "").strip()
        parts.append(escape(caption) if html_links else caption)
        if result_payload.get("url"):
            if html_links:
                parts.append(
                    _format_media_link(kind=str(kind), url=str(result_payload["url"]))
                )
            else:
                parts.append(str(result_payload["url"]).strip())
        if result_payload.get("revised_prompt"):
            revised_prompt = str(result_payload["revised_prompt"])
            if html_links:
                revised_prompt = escape(revised_prompt)
            parts.append(f"Уточнённый prompt: {revised_prompt}")
    elif kind == "tts":
        parts.append(str(result_payload.get("message") or "").strip())
        if result_payload.get("url"):
            parts.append(str(result_payload["url"]).strip())
    else:
        parts.append(str(result_payload))
    return "\n".join(part for part in parts if part).strip() or "Результат сохранён."


def _format_history(rows: list[Dict[str, Any]]) -> str:
    if not rows:
        return "История пока пустая."
    lines = ["Последние генерации:"]
    for row in rows:
        prompt = _short_history_text(_generation_prompt_text(row))
        lines.append(
            "\n"
            f"#{row['id']} Модель - {row['model_display_name']}\n"
            f"      Статус - {row['status']}\n"
            f"      Списано - {format_coin_amount(row.get('coins_charged'))}\n"
            f"      Промпт - {prompt}"
        )
    lines.extend(
        [
            "",
            "Чтобы увидеть результат выберите кнопку с номером генерации:",
        ]
    )
    return "\n".join(lines)


def _format_history_result(
    row: Dict[str, Any], *, include_media_link: bool = True
) -> str:
    html_links = include_media_link and _generation_result_has_media_link(row)
    result_text = _generation_result_text(row, html_links=html_links)
    if not html_links:
        result_text = _short_history_text(result_text, limit=2400)
    prompt = _short_history_text(_generation_prompt_text(row), limit=800)
    model_name = str(row["model_display_name"])
    status = str(row["status"])
    if html_links:
        prompt = escape(prompt)
        model_name = escape(model_name)
        status = escape(status)
    return (
        f"Генерация #{row['id']}\n\n"
        f"Модель - {model_name}\n"
        f"Статус - {status}\n"
        f"Списано - {format_coin_amount(row.get('coins_charged'))}\n"
        f"Промпт - {prompt}\n\n"
        f"Результат:\n{result_text}"
    )


def _history_image_sources(result: Dict[str, Any]) -> list[Any]:
    sources: list[Any] = []
    file_id = str(result.get("telegram_photo_file_id") or "").strip()
    if file_id:
        sources.append(file_id)
    if result.get("image_b64"):
        try:
            image_bytes = base64.b64decode(str(result["image_b64"]), validate=True)
            sources.append(
                BufferedInputFile(
                    image_bytes,
                    filename=str(result.get("file_name") or "cea-ai.png"),
                )
            )
        except (binascii.Error, ValueError):
            pass
    url = str(result.get("url") or "").strip()
    if url:
        sources.append(url)
    return sources


def _history_video_sources(result: Dict[str, Any]) -> list[str]:
    sources: list[str] = []
    file_id = str(result.get("telegram_video_file_id") or "").strip()
    if file_id:
        sources.append(file_id)
    url = str(result.get("url") or "").strip()
    if url:
        sources.append(url)
    return sources


def _history_audio_sources(result: Dict[str, Any]) -> list[Any]:
    sources: list[Any] = []
    file_id = str(result.get("telegram_audio_file_id") or "").strip()
    if file_id:
        sources.append(file_id)
    if result.get("audio_b64"):
        try:
            audio_bytes = base64.b64decode(str(result["audio_b64"]), validate=True)
            sources.append(
                BufferedInputFile(
                    audio_bytes,
                    filename=str(result.get("file_name") or "cea-ai-voice.mp3"),
                )
            )
        except (binascii.Error, ValueError):
            pass
    return sources


async def _show_history_media_result(
    message: Message,
    services: AppServices,
    user_id: int,
    generation: Dict[str, Any],
    *,
    page: int,
    delete_current: bool = False,
) -> bool:
    result = _generation_result_payload(generation)
    kind = str(result.get("kind") or "")
    if kind not in {"image", "video", "tts"}:
        return False

    if kind == "image":
        sources = _history_image_sources(result)
    elif kind == "video":
        sources = _history_video_sources(result)
    else:
        sources = _history_audio_sources(result)
    if not sources:
        return False

    state, payload = _session_state_payload(services, user_id)
    tracked_ids = _tracked_message_ids(payload)
    reply_markup = history_result_keyboard(page=page)
    await _remove_legacy_reply_keyboard(message, payload, reply_markup)
    if tracked_ids:
        await _delete_screen_messages(message, tracked_ids)
    elif delete_current and message.message_id and not _is_user_message(message):
        try:
            await message.bot.delete_message(
                chat_id=message.chat.id,
                message_id=message.message_id,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            pass

    caption = _telegram_caption(
        _format_history_result(generation, include_media_link=False)
    )
    for source in sources:
        try:
            if kind == "image":
                sent = await message.bot.send_photo(
                    chat_id=message.chat.id,
                    photo=source,
                    caption=caption,
                    reply_markup=reply_markup,
                )
                await _remember_generation_media_file(
                    services,
                    generation_id=int(generation["id"]),
                    kind="image",
                    file_id=_sent_photo_file_id(sent),
                )
            elif kind == "video":
                sent = await message.bot.send_video(
                    chat_id=message.chat.id,
                    video=str(source),
                    caption=caption,
                    reply_markup=reply_markup,
                )
                await _remember_generation_media_file(
                    services,
                    generation_id=int(generation["id"]),
                    kind="video",
                    file_id=_sent_video_file_id(sent),
                )
            else:
                sent = await message.bot.send_audio(
                    chat_id=message.chat.id,
                    audio=source,
                    caption=caption,
                    reply_markup=reply_markup,
                )
                await _remember_generation_media_file(
                    services,
                    generation_id=int(generation["id"]),
                    kind="tts",
                    file_id=_sent_audio_file_id(sent),
                )
            _remember_screen_message(
                services,
                user_id,
                state=state,
                payload=payload,
                message_id=sent.message_id,
                reply_markup=reply_markup,
                is_media=True,
            )
            return True
        except (TelegramBadRequest, TelegramForbiddenError, ValueError):
            continue
    return False


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
    text = _format_menu(profile_user, subscription)
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


async def _send_admin_home(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    delete_current: bool = False,
    notice: str | None = None,
) -> None:
    maintenance_active = services.admin.is_maintenance_mode_active()
    maintenance_status = "включены" if maintenance_active else "выключены"
    text = (
        "🛠 Админка CeaAI\n\n"
        f"Статус техработ: {maintenance_status}.\n"
        "Выберите раздел."
    )
    if notice:
        text = f"{notice}\n\n{text}"
    await _show_screen(
        message,
        services,
        user_id,
        text,
        reply_markup=admin_menu_keyboard(
            maintenance_active=maintenance_active
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
        reply_markup=_main_menu_keyboard(services, user_id),
        delete_current=delete_current,
    )


def _main_menu_keyboard(
    services: AppServices, user_id: int
) -> InlineKeyboardMarkup:
    user = services.users.get_by_id(user_id)
    is_admin = bool(user and services.admin.has_admin_access(user))
    gift_claimed = False
    if not is_admin:
        gift_claimed = services.subscriptions.has_channel_gift(
            user_id,
            gift_key=GIFT_CHANNEL_USERNAME,
        )
    return main_menu_keyboard(
        gift_claimed=gift_claimed,
        support_username=services.settings.support_username,
    )


async def _send_work_menu(
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
        _format_work_menu(),
        reply_markup=work_menu_keyboard(),
        delete_current=delete_current,
    )


async def _check_gift_channel_subscription(bot: Any, telegram_id: int) -> tuple[bool, bool]:
    try:
        member = await bot.get_chat_member(
            chat_id=GIFT_CHANNEL_CHAT_ID,
            user_id=telegram_id,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        return False, False

    status = getattr(getattr(member, "status", ""), "value", getattr(member, "status", ""))
    if status in {"creator", "administrator", "member"}:
        return True, True
    if status == "restricted" and bool(getattr(member, "is_member", False)):
        return True, True
    return True, False


async def _send_gift(
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
        _format_gift_screen(),
        reply_markup=gift_subscription_keyboard(info_channel_url=GIFT_CHANNEL_URL),
        delete_current=delete_current,
        parse_mode="HTML",
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
    page: int = 1,
    delete_current: bool = False,
) -> None:
    total = services.generations.count_for_user(user_id=user_id)
    pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = max(1, min(page, pages))
    rows = services.generations.list_recent(
        user_id=user_id,
        limit=HISTORY_PAGE_SIZE,
        offset=(page - 1) * HISTORY_PAGE_SIZE,
    )
    await _show_screen(
        message,
        services,
        user_id,
        _format_history(rows),
        reply_markup=history_keyboard(rows, page=page, pages=pages),
        delete_current=delete_current,
    )


async def _send_history_result(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    generation_id: int,
    page: int = 1,
    delete_current: bool = False,
) -> None:
    generation = services.generations.get_for_user(
        user_id=user_id,
        generation_id=generation_id,
    )
    if generation is None:
        await _send_history(
            message,
            services,
            user_id,
            page=page,
            delete_current=delete_current,
        )
        return
    if await _show_history_media_result(
        message,
        services,
        user_id,
        generation,
        page=page,
        delete_current=delete_current,
    ):
        return
    has_media_link = _generation_result_has_media_link(generation)
    await _show_screen(
        message,
        services,
        user_id,
        _format_history_result(generation),
        reply_markup=history_result_keyboard(page=page),
        delete_current=delete_current,
        parse_mode="HTML" if has_media_link else None,
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
        reply_markup=back_to_menu_keyboard(),
        delete_current=delete_current,
    )


async def _send_about_service(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    delete_current: bool = False,
) -> None:
    _clear_dialog_state(services, user_id)
    support_username = services.settings.support_username or "cea_help"
    public_offer_url = (
        services.settings.public_offer_url.strip() or DEFAULT_PUBLIC_OFFER_URL
    )
    privacy_policy_url = (
        services.settings.privacy_policy_url.strip() or DEFAULT_PRIVACY_POLICY_URL
    )
    await _show_screen(
        message,
        services,
        user_id,
        "🛡 <b>О сервисе Cea AI</b>\n\n"
        "Cea AI — нейросети для текста, изображений, видео и озвучки "
        "в одном Telegram-боте.\n\n"
        f"📢 Канал: @{GIFT_CHANNEL_USERNAME}\n"
        f"🆘 Поддержка: @{support_username}",
        reply_markup=about_service_keyboard(
            public_offer_url=public_offer_url,
            privacy_policy_url=privacy_policy_url,
            support_username=support_username,
        ),
        delete_current=delete_current,
        parse_mode="HTML",
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


async def _send_tts_voice_preview(
    message: Message,
    services: AppServices,
    user_id: int,
    *,
    delete_current: bool = False,
) -> None:
    model = next(
        (
            item
            for item in services.catalog.list_models()
            if item["generation_type"] == "tts"
        ),
        None,
    )
    if model is None:
        await _show_screen(
            message,
            services,
            user_id,
            "Для озвучки пока нет активной модели.",
            reply_markup=back_to_menu_keyboard(),
            delete_current=delete_current,
        )
        return
    sample_paths = [
        TTS_VOICE_SAMPLES_DIR / f"{voice}.wav" for _, voice in TTS_VOICES
    ]
    if not all(path.is_file() for path in sample_paths):
        await _show_screen(
            message,
            services,
            user_id,
            "Пример голосов временно недоступен.",
            reply_markup=back_to_menu_keyboard(),
            delete_current=delete_current,
        )
        return
    _, payload = _session_state_payload(services, user_id)
    if delete_current:
        await _delete_screen_messages(message, _tracked_message_ids(payload))
    try:
        sent_media = await message.bot.send_media_group(
            chat_id=message.chat.id,
            media=[
                InputMediaAudio(
                    media=FSInputFile(path),
                    caption=f"🎙 {index}. {voice_label}",
                )
                for index, ((voice_label, _), path) in enumerate(
                    zip(TTS_VOICES, sample_paths), start=1
                )
            ],
        )
        chooser = await message.bot.send_message(
            chat_id=message.chat.id,
            text="Выберите понравившийся голос:",
            reply_markup=tts_voice_keyboard(),
        )
    except (TelegramBadRequest, TelegramForbiddenError, ValueError) as exc:
        record_error(exception=exc)
        _reset_dialog_state(services, user_id)
        await _show_screen(
            message,
            services,
            user_id,
            "Не удалось загрузить пример голосов. Попробуйте ещё раз позже.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    services.users.set_session(
        user_id,
        state="waiting_tts_voice",
        payload={
            "model_price_id": int(model["id"]),
            LAST_BOT_MESSAGE_IDS: [
                *(item.message_id for item in sent_media),
                chooser.message_id,
            ],
            LAST_SCREEN_HAS_MEDIA: True,
        },
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

    if text_lower in {
        "профиль",
        "мой профиль",
        "/профиль",
        "profile",
        "/profile",
    } or text == PROFILE_BUTTON:
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

    if text_lower == "забрать подарок" or text == GIFT_BUTTON:
        _clear_dialog_state(services, user["id"])
        await _send_gift(message, services, user["id"], delete_current=True)
        return True

    if text_lower == "начать работу" or text == START_WORK_BUTTON:
        _clear_dialog_state(services, user["id"])
        await _send_work_menu(message, services, user["id"], delete_current=True)
        return True

    if text_lower in {"заработать", "реферальная программа"} or text == REFERRAL_BUTTON:
        _clear_dialog_state(services, user["id"])
        await _send_referral(message, services, user["id"], delete_current=True)
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
        await _send_models_for_types(
            message,
            services,
            user["id"],
            generation_types={"video"},
            title="Выберите модель для видео с AI.",
            skip_single_model_choice=True,
            delete_current=True,
        )
        return True

    if text_lower in {"озвучка с ai", "озвучка текста"} or text == VOICE_AI_BUTTON:
        await _send_tts_voice_preview(
            message,
            services,
            user["id"],
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
        await _send_referral_join_notice(message, referral_result)
        _reset_dialog_state(services, user["id"])
        await _send_menu_screen(message, services, user["id"], delete_current=True)

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

    @router.callback_query(F.data == "menu:gift")
    async def menu_gift(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        _clear_dialog_state(services, user["id"])
        if callback.message:
            await _send_gift(
                callback.message, services, user["id"], delete_current=True
            )
        await callback.answer()

    @router.callback_query(F.data == "gift:check")
    async def gift_check(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        if not callback.message:
            await callback.answer("Откройте меню подарка ещё раз.", show_alert=True)
            return

        can_check, is_subscribed = await _check_gift_channel_subscription(
            callback.bot,
            callback.from_user.id,
        )
        if not can_check:
            await _show_screen(
                callback.message,
                services,
                user["id"],
                _format_gift_check_unavailable(),
                reply_markup=gift_subscription_keyboard(
                    info_channel_url=GIFT_CHANNEL_URL
                ),
            )
            await callback.answer()
            return
        if not is_subscribed:
            await _show_screen(
                callback.message,
                services,
                user["id"],
                _format_gift_not_subscribed(),
                reply_markup=gift_subscription_keyboard(
                    info_channel_url=GIFT_CHANNEL_URL
                ),
            )
            await callback.answer()
            return

        result = services.subscriptions.grant_channel_gift(
            user_id=user["id"],
            plan_code=GIFT_PLAN_CODE,
            duration_days=GIFT_DURATION_DAYS,
            coins_amount=GIFT_COINS_AMOUNT,
            gift_key=GIFT_CHANNEL_USERNAME,
        )
        await _show_screen(
            callback.message,
            services,
            user["id"],
            (
                _format_gift_activated(result)
                if result["created"]
                else _format_gift_already_claimed(result)
            ),
            reply_markup=back_to_menu_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()

    @router.callback_query(F.data == "menu:work")
    async def menu_work(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        _clear_dialog_state(services, user["id"])
        if callback.message:
            subscription = services.subscriptions.active_for_user(user["id"])
            gift_claimed = services.subscriptions.has_channel_gift(
                user["id"], gift_key=GIFT_CHANNEL_USERNAME
            )
            if subscription is None:
                await _show_screen(
                    callback.message,
                    services,
                    user["id"],
                    _work_access_required_message(),
                    reply_markup=(
                        subscription_required_keyboard()
                        if gift_claimed
                        else work_access_required_keyboard()
                    ),
                    delete_current=True,
                )
                await callback.answer()
                return
            await _send_work_menu(
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
                "Если нужно остановить будущие списания, напишите в поддержку.",
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
        elif payment["provider"] == "platega" and selected_plan is not None:
            payment_text = _format_platega_payment_screen(
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
            "video": ("Выберите модель для видео с AI.", True),
            "tts": ("Выберите модель для озвучки текста.", True),
        }.get(generation_type)
        if config is None:
            await callback.answer("Неизвестный раздел", show_alert=True)
            return
        if generation_type == "tts":
            _clear_dialog_state(services, user["id"])
            await _send_tts_voice_preview(
                callback.message,
                services,
                user["id"],
                delete_current=True,
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

    @router.callback_query(F.data.startswith("tts:voice:"))
    async def choose_tts_voice(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        if not callback.message or not callback.data:
            await callback.answer()
            return
        voice = callback.data.rsplit(":", 1)[-1]
        allowed_voices = {voice_key for _, voice_key in TTS_VOICES}
        state, payload = _session_state_payload(services, user["id"])
        model_price_id = int(payload.get("model_price_id", 0))
        if state != "waiting_tts_voice" or voice not in allowed_voices:
            await callback.answer("Сначала выберите голос заново.", show_alert=True)
            return
        model = services.catalog.get_model(model_price_id)
        if model is None or model["generation_type"] != "tts":
            await callback.answer("Модель озвучки недоступна.", show_alert=True)
            return

        await callback.answer()
        await _delete_screen_messages(callback.message, _tracked_message_ids(payload))
        services.users.set_session(
            user["id"],
            state="waiting_prompt",
            payload={
                "model_price_id": model_price_id,
                "tts_voice": voice,
            },
        )
        await _show_screen(
            callback.message,
            services,
            user["id"],
            _format_tts_prompt_screen(model, voice=voice),
            reply_markup=back_to_menu_keyboard(),
        )

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

    @router.callback_query(F.data.startswith("history:"))
    async def history_callback(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        if not callback.message or not callback.data:
            await callback.answer()
            return

        parts = callback.data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        try:
            if action == "page":
                page = int(parts[2]) if len(parts) > 2 else 1
                await _send_history(
                    callback.message,
                    services,
                    user["id"],
                    page=page,
                )
            elif action == "view":
                generation_id = int(parts[2]) if len(parts) > 2 else 0
                page = int(parts[3]) if len(parts) > 3 else 1
                await _send_history_result(
                    callback.message,
                    services,
                    user["id"],
                    generation_id=generation_id,
                    page=page,
                )
            else:
                await callback.answer("Неизвестное действие", show_alert=True)
                return
        except ValueError:
            await callback.answer("Не получилось открыть историю.", show_alert=True)
            return
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

    @router.callback_query(F.data == "menu:about")
    async def menu_about(callback: CallbackQuery) -> None:
        user = services.users.ensure_telegram_user(**_user_kwargs(callback))
        if _is_blocked_regular_user(services, user):
            if callback.message:
                await _send_blocked_notice(callback.message, services, user["id"])
            await callback.answer()
            return
        if callback.message:
            await _send_about_service(
                callback.message, services, user["id"], delete_current=True
            )
        await callback.answer()

    @router.callback_query(F.data == "promo:placeholder")
    async def promo_placeholder(callback: CallbackQuery) -> None:
        await callback.answer(
            "Промокоды будут подключены на следующем этапе.",
            show_alert=True,
        )

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
                    rate_percent=referral_stats.rate_percent,
                    withdrawal_min_kopecks=referral_stats.withdrawal_min_kopecks,
                ),
                reply_markup=referral_keyboard(_referral_link(user)),
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
            await _send_menu_screen(message, services, user["id"], delete_current=True)
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
                generation = await asyncio.to_thread(
                    services.generations.generate,
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
                reply_markup=None,
                generation_id=int(generation.generation["id"]),
            )
            await _send_main_menu_followup(message, services, user["id"])
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
            (
                "Запускаю генерацию видео. Это может занять несколько минут..."
                if model["generation_type"] == "video"
                else "Запускаю генерацию..."
            ),
            reply_markup=None,
            delete_current=True,
        )
        try:
            generation = await asyncio.to_thread(
                services.generations.generate,
                user_id=user["id"],
                model_price_id=model_price_id,
                prompt_text=prompt_text,
                image_input=image_input,
                tts_voice=str(payload.get("tts_voice") or "") or None,
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
                if generation.model["generation_type"] in {"image", "video", "tts"}
                else back_to_menu_keyboard()
            ),
            image_caption=(
                _format_media_generation_caption(
                    prompt_text=prompt_text,
                    model=generation.model,
                    coins_charged=generation.generation["coins_charged"],
                    balance_after=generation.balance_after,
                )
                if generation.model["generation_type"] == "image"
                else None
            ),
            video_caption=(
                _format_video_generation_result(
                    prompt_text=prompt_text,
                    model=generation.model,
                    coins_charged=generation.generation["coins_charged"],
                    balance_after=generation.balance_after,
                    result=generation.result,
                )
                if generation.model["generation_type"] == "video"
                else None
            ),
            audio_caption=(
                _format_media_generation_caption(
                    prompt_text=prompt_text,
                    model=generation.model,
                    coins_charged=generation.generation["coins_charged"],
                    balance_after=generation.balance_after,
                )
                if generation.model["generation_type"] == "tts"
                else None
            ),
            generation_id=int(generation.generation["id"]),
            send_menu_followup=(
                generation.model["generation_type"] in {"image", "video", "tts"}
            ),
        )

    return router
