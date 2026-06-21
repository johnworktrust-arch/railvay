from __future__ import annotations

from typing import Any, Dict


BASE_TEXT_INSTRUCTIONS = (
    "Ты полезный AI-помощник внутри Telegram-бота CeaAI. "
    "Отвечай кратко, понятно и по-русски, если пользователь не попросил иначе."
)


def model_identity_name(model: Dict[str, Any]) -> str:
    provider = str(model.get("provider") or "").lower()
    display_name = str(model.get("display_name") or "")
    model_key = str(model.get("model_key") or "")

    if provider == "openai" or "chatgpt" in display_name.lower():
        return "ChatGPT"
    if provider == "deepseek" or "deepseek" in display_name.lower():
        return "DeepSeek"
    return display_name or model_key or "AI-модель"


def text_model_instructions(
    model: Dict[str, Any], *, system_prompt: str | None = None
) -> str:
    identity = model_identity_name(model)
    instructions = (
        f"{BASE_TEXT_INSTRUCTIONS}\n\n"
        f"Ты работаешь как {identity} внутри Cea AI. "
        f"Если пользователь спрашивает, кто ты, какая ты модель или ChatGPT ты "
        f"или DeepSeek, отвечай честно: ты {identity} в Telegram-боте Cea AI."
    )
    if system_prompt:
        instructions = f"{instructions}\n\n{system_prompt.strip()}"
    return instructions
