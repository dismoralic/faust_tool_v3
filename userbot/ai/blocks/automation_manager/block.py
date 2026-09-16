from __future__ import annotations

from telethon.utils import get_peer_id

from userbot.ai.brain import ai_function
from userbot.core.entities import resolve_entity


@ai_function(
    description="Включает или выключает AI-автоответ для выбранной области",
    triggers=["включи автоответ", "выключи автоответ", "настрой автоответчик", "включи автоответчик", "выключи автоответчик"],
    params=[
        {"name": "mode", "type": "string", "required": True, "enum": ["persona", "assistant"]},
        {"name": "scope", "type": "string", "required": True, "enum": ["all", "user", "chat", "contacts", "noncontacts"]},
        {"name": "enabled", "type": "boolean", "required": True},
        {"name": "target", "type": "string", "description": "ID, @username или имя для user/chat"},
    ],
    risk="write",
    owner_only=True,
    name="set_ai_autoreply",
)
async def set_ai_autoreply(
    mode: str,
    scope: str,
    enabled: bool,
    target: str = None,
    client=None,
    message=None,
    brain=None,
):
    normalized_scope = str(scope).lower()
    resolved_target = target
    if normalized_scope in {"chat", "group"} and target in (None, ""):
        resolved_target = getattr(message, "chat_id", None)
    elif normalized_scope in {"user", "users"} and target in (None, ""):
        if message is not None and getattr(message, "is_reply", False):
            reply = await message.get_reply_message()
            resolved_target = getattr(reply, "sender_id", None)
    if normalized_scope in {"chat", "group", "user", "users"} and resolved_target not in (None, ""):
        if not str(resolved_target).lstrip("-").isdigit():
            entity = await resolve_entity(
                client,
                resolved_target,
                message=message,
                user_only=normalized_scope in {"user", "users"},
            )
            resolved_target = entity.id if normalized_scope in {"user", "users"} else get_peer_id(entity)
    return brain.configure_autoreply(mode, scope, enabled, resolved_target)


@ai_function(
    description="Показывает текущие режимы и области AI-автоответчика",
    triggers=["покажи автоответчик", "статус автоответчика", "где включен автоответ"],
    risk="read",
    name="ai_autoreply_status",
)
async def ai_autoreply_status(brain=None):
    return brain.autoreply_status_text()
