from __future__ import annotations

from typing import Any, Dict, List

from ceai.database import Database
from ceai.repositories.model_prices import ModelPriceRepository
from ceai.repositories.text_chats import TextChatRepository
from ceai.services.exceptions import BusinessRuleError, NotFoundError


DEFAULT_TEXT_CHAT_TITLES = ("Основной", "Медицина", "Работа", "Психолог", "Спорт")
DEFAULT_TEXT_CHAT_PROMPTS = {
    "Основной": (
        "Ты универсальный AI-ассистент Cea AI. Отвечай ясно, полезно и по делу."
    ),
    "Медицина": (
        "Ты AI-помощник по медицинским вопросам. Объясняй аккуратно и понятно, "
        "не ставь диагнозы, не назначай лечение и рекомендуй обратиться к врачу "
        "при симптомах, рисках или срочных состояниях."
    ),
    "Работа": (
        "Ты AI-ассистент для рабочих задач: помогаешь с планированием, письмами, "
        "идеями, структурой документов, переговорами и продуктивностью."
    ),
    "Психолог": (
        "Ты бережный AI-психологический ассистент. Поддерживай пользователя, "
        "помогай разложить ситуацию и эмоции, но не заменяй психотерапевта "
        "и советуй обратиться к специалисту при кризисных состояниях."
    ),
    "Спорт": (
        "Ты AI-помощник по спорту и тренировкам. Давай понятные рекомендации "
        "по упражнениям, режиму и восстановлению, учитывай безопасность и "
        "советуй врача или тренера при травмах и ограничениях."
    ),
}
RESERVED_TEXT_CHAT_TITLES = (
    "➕ Добавить чат",
    "🗑 Удалить текущий чат",
    "⬅️ В меню",
    "К чатам",
)
CUSTOM_TEXT_CHAT_PROMPT = (
    "Ты AI-ассистент в пользовательском чате Cea AI. Учитывай название чата "
    "как контекст и отвечай по теме запроса."
)


class TextChatService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.chats = TextChatRepository()
        self.models = ModelPriceRepository()

    def list_for_model(
        self, *, user_id: int, model_price_id: int
    ) -> List[Dict[str, Any]]:
        with self.db.transaction() as conn:
            self._ensure_text_model(conn, model_price_id)
            self._ensure_default_chats(conn, user_id, model_price_id)
            return self._decorate_chats(
                self.chats.list_active_for_model(
                    conn, user_id=user_id, model_price_id=model_price_id
                )
            )

    def default_for_model(self, *, user_id: int, model_price_id: int) -> Dict[str, Any]:
        chats = self.list_for_model(user_id=user_id, model_price_id=model_price_id)
        for chat in chats:
            if chat["title"] == DEFAULT_TEXT_CHAT_TITLES[0]:
                return chat
        if not chats:
            raise RuntimeError("Default text chat was not created")
        return chats[0]

    def get_active(self, *, user_id: int, chat_id: int) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            chat = self.chats.get_active_for_user(conn, user_id=user_id, chat_id=chat_id)
            if chat is None:
                raise NotFoundError("Чат не найден")
            return self._decorate_chat(chat)

    def create_custom(
        self, *, user_id: int, model_price_id: int, title: str
    ) -> Dict[str, Any]:
        normalized = " ".join(title.strip().split())
        if not normalized:
            raise BusinessRuleError("Введите название чата")
        if len(normalized) > 40:
            raise BusinessRuleError("Название чата должно быть до 40 символов")
        if normalized in RESERVED_TEXT_CHAT_TITLES:
            raise BusinessRuleError("Выберите другое название чата")
        with self.db.transaction() as conn:
            self._ensure_text_model(conn, model_price_id)
            self._ensure_default_chats(conn, user_id, model_price_id)
            existing = self.chats.find_active_by_title(
                conn,
                user_id=user_id,
                model_price_id=model_price_id,
                title=normalized,
            )
            if existing is not None:
                raise BusinessRuleError("Чат с таким названием уже есть")
            return self._decorate_chat(
                self.chats.create(
                    conn,
                    user_id=user_id,
                    model_price_id=model_price_id,
                    title=normalized,
                    is_default=False,
                )
            )

    def delete(self, *, user_id: int, chat_id: int) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            chat = self.chats.get_active_for_user(conn, user_id=user_id, chat_id=chat_id)
            if chat is None:
                raise NotFoundError("Чат не найден")
            if chat["is_default"]:
                raise BusinessRuleError("Стандартный чат нельзя удалить")
            self.chats.soft_delete(conn, chat_id)
            self._ensure_default_chats(conn, user_id, int(chat["model_price_id"]))
            fallback = self.chats.find_active_by_title(
                conn,
                user_id=user_id,
                model_price_id=int(chat["model_price_id"]),
                title=DEFAULT_TEXT_CHAT_TITLES[0],
            )
            if fallback is None:
                raise RuntimeError("Default text chat was not found")
            return self._decorate_chat(fallback)

    def _ensure_text_model(self, conn: Any, model_price_id: int) -> Dict[str, Any]:
        model = self.models.get_by_id(conn, model_price_id)
        if model is None or not model["is_active"]:
            raise NotFoundError("Модель не найдена")
        if model["generation_type"] != "text":
            raise BusinessRuleError("Чаты доступны только для текстовых моделей")
        return model

    def _ensure_default_chats(
        self, conn: Any, user_id: int, model_price_id: int
    ) -> None:
        for title in DEFAULT_TEXT_CHAT_TITLES:
            existing = self.chats.find_active_by_title(
                conn,
                user_id=user_id,
                model_price_id=model_price_id,
                title=title,
            )
            if existing is None:
                self.chats.create(
                    conn,
                    user_id=user_id,
                    model_price_id=model_price_id,
                    title=title,
                    is_default=True,
                )

    def _decorate_chats(self, chats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [self._decorate_chat(chat) for chat in chats]

    def _decorate_chat(self, chat: Dict[str, Any]) -> Dict[str, Any]:
        decorated = dict(chat)
        decorated["system_prompt"] = DEFAULT_TEXT_CHAT_PROMPTS.get(
            str(chat["title"]), CUSTOM_TEXT_CHAT_PROMPT
        )
        return decorated
