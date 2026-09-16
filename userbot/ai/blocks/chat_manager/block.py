from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from telethon.tl.types import Channel, User

from userbot.ai.brain import ai_function
from userbot.core.entities import resolve_entity


BLOCK_INFO = {
    "name": "chat_manager",
    "description": "Безопасное чтение и управление диалогами",
}


async def _entity(client: Any, message: Any, target: Optional[Any]):
    return await resolve_entity(client, target, message=message)


def _name(entity: Any) -> str:
    if isinstance(entity, User):
        full = " ".join(x for x in (entity.first_name, entity.last_name) if x)
        return full or entity.username or str(entity.id)
    return getattr(entity, "title", None) or str(getattr(entity, "id", "неизвестно"))


def _message_line(msg: Any) -> str:
    text = " ".join((msg.raw_text or "[медиа]").split())
    if len(text) > 180:
        text = text[:177] + "..."
    sender = getattr(msg, "sender_id", None) or "?"
    date = msg.date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"{msg.id} | {date} | {sender}: {text}"


@ai_function(
    description="Показывает информацию о пользователе или текущем чате",
    triggers=["покажи информацию о чате", "информация о чате", "информация о пользователе", "кто это"],
    params=[{"name": "target", "type": "string", "description": "@username или ID"}],
    risk="read",
    name="chat_info",
)
async def chat_info(target: str = None, client=None, message=None):
    entity = await _entity(client, message, target)
    kind = "пользователь" if isinstance(entity, User) else "канал" if isinstance(entity, Channel) else "группа"
    username = getattr(entity, "username", None)
    members = getattr(entity, "participants_count", None)
    lines = [f"Тип: {kind}", f"Название: {_name(entity)}", f"ID: {entity.id}"]
    if username:
        lines.append(f"Username: @{username}")
    if members is not None:
        lines.append(f"Участников: {members}")
    return "\n".join(lines)


@ai_function(
    description="Показывает последние сообщения выбранного диалога",
    triggers=["покажи последние сообщения", "последние сообщения", "что писали в чате"],
    params=[
        {"name": "target", "type": "string", "description": "@username или ID"},
        {"name": "limit", "type": "integer", "default": 5, "min": 1, "max": 20},
    ],
    risk="read",
    name="recent_messages",
)
async def recent_messages(target: str = None, limit: int = 5, client=None, message=None):
    entity = await _entity(client, message, target)
    messages = [item async for item in client.iter_messages(entity, limit=limit)]
    if not messages:
        return f"В чате с {_name(entity)} сообщений не найдено."
    return f"Последние сообщения — {_name(entity)}:\n" + "\n".join(_message_line(x) for x in messages)


@ai_function(
    description="Ищет сообщения по тексту в выбранном диалоге",
    triggers=["найди сообщения", "поиск сообщений", "найди в чате"],
    params=[
        {"name": "query", "type": "string", "required": True, "max_length": 200},
        {"name": "target", "type": "string", "description": "@username или ID"},
        {"name": "limit", "type": "integer", "default": 10, "min": 1, "max": 20},
    ],
    risk="read",
    name="search_messages",
)
async def search_messages(query: str, target: str = None, limit: int = 10, client=None, message=None):
    entity = await _entity(client, message, target)
    messages = [item async for item in client.iter_messages(entity, search=query, limit=limit)]
    if not messages:
        return f"По запросу {query!r} ничего не найдено."
    return f"Результаты поиска — {_name(entity)}:\n" + "\n".join(_message_line(x) for x in messages)


@ai_function(
    description="Отправляет указанное сообщение в выбранный диалог",
    triggers=["отправь сообщение", "напиши пользователю", "напиши в чат"],
    params=[
        {"name": "text", "type": "string", "required": True, "max_length": 3500},
        {"name": "target", "type": "string", "description": "@username или ID"},
    ],
    risk="write",
    confirmation=True,
    owner_only=True,
    name="send_message",
)
async def send_message(text: str, target: str = None, client=None, message=None):
    entity = await _entity(client, message, target)
    sent = await client.send_message(entity, text, parse_mode=None)
    return f"Сообщение отправлено в {_name(entity)}. ID сообщения: {sent.id}"


@ai_function(
    description="Удаляет только сообщения владельца юзербота в выбранном диалоге",
    triggers=["удали мои сообщения", "почисти мои сообщения", "удали что я писал"],
    params=[
        {"name": "target", "type": "string", "description": "@username или ID"},
        {"name": "minutes", "type": "integer", "default": 0, "min": 0, "max": 10080},
        {"name": "max_count", "type": "integer", "default": 100, "min": 1, "max": 1000},
    ],
    risk="destructive",
    owner_only=True,
    name="delete_my_messages",
)
async def delete_my_messages(
    target: str = None,
    minutes: int = 0,
    max_count: int = 100,
    client=None,
    message=None,
):
    entity = await _entity(client, message, target)
    me = await client.get_me()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes) if minutes else None
    ids = []
    scanned = 0
    async for item in client.iter_messages(entity):
        scanned += 1
        if cutoff and item.date < cutoff:
            break
        if item.sender_id == me.id:
            ids.append(item.id)
            if len(ids) >= max_count:
                break
        if scanned >= max(max_count * 20, 2000):
            break
    for start in range(0, len(ids), 100):
        await client.delete_messages(entity, ids[start:start + 100], revoke=True)
    return f"Удалено моих сообщений: {len(ids)}. Чат: {_name(entity)}"


@ai_function(
    description="Полностью удаляет личный диалог и его историю",
    triggers=["удали личный диалог", "удали диалог", "очисти личную переписку"],
    params=[{"name": "target", "type": "string", "description": "@username или ID"}],
    risk="destructive",
    owner_only=True,
    name="delete_private_dialog",
)
async def delete_private_dialog(target: str = None, client=None, message=None):
    entity = await _entity(client, message, target)
    if not isinstance(entity, User):
        raise ValueError("Полное удаление разрешено только для личных диалогов")
    await client.delete_dialog(entity, revoke=True)
    return f"Личный диалог с {_name(entity)} удалён."
