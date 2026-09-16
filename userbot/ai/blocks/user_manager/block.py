from __future__ import annotations

from telethon import functions
from telethon.tl.types import User

from userbot.ai.brain import ai_function
from userbot.core.entities import resolve_entity


BLOCK_INFO = {
    "name": "user_manager",
    "description": "Информация и управление пользователями",
}


async def _user(client, message, target):
    if target:
        entity = await resolve_entity(client, target, message=message, user_only=True)
    elif message is not None and getattr(message, "is_reply", False):
        reply = await message.get_reply_message()
        entity = await reply.get_sender() if reply else None
    elif message is not None:
        entity = await resolve_entity(client, None, message=message, user_only=True)
    else:
        entity = None
    if not isinstance(entity, User):
        raise ValueError("Укажи пользователя через @username, ID или реплай")
    return entity


def _name(user):
    return " ".join(x for x in (user.first_name, user.last_name) if x) or user.username or str(user.id)


@ai_function(
    description="Показывает основные данные пользователя",
    triggers=["покажи пользователя", "информация о юзере", "данные пользователя"],
    params=[{"name": "target", "type": "string", "description": "@username или ID"}],
    risk="read",
    name="user_info",
)
async def user_info(target: str = None, client=None, message=None):
    user = await _user(client, message, target)
    full = await client(functions.users.GetFullUserRequest(user))
    about = getattr(full.full_user, "about", None)
    lines = [
        f"Имя: {_name(user)}",
        f"ID: {user.id}",
        f"Username: @{user.username}" if user.username else "Username: нет",
        f"Бот: {'да' if user.bot else 'нет'}",
    ]
    if about:
        lines.append(f"О себе: {about[:500]}")
    return "\n".join(lines)


@ai_function(
    description="Блокирует пользователя в Telegram",
    triggers=["заблокируй пользователя", "добавь в черный список", "заблокируй юзера"],
    params=[{"name": "target", "type": "string", "required": True, "description": "@username или ID"}],
    risk="destructive",
    owner_only=True,
    name="block_user",
)
async def block_user(target: str, client=None, message=None):
    user = await _user(client, message, target)
    await client(functions.contacts.BlockRequest(id=user))
    return f"Пользователь {_name(user)} заблокирован."


@ai_function(
    description="Разблокирует пользователя в Telegram",
    triggers=["разблокируй пользователя", "убери из черного списка", "разблокируй юзера"],
    params=[{"name": "target", "type": "string", "required": True, "description": "@username или ID"}],
    risk="write",
    confirmation=True,
    owner_only=True,
    name="unblock_user",
)
async def unblock_user(target: str, client=None, message=None):
    user = await _user(client, message, target)
    await client(functions.contacts.UnblockRequest(id=user))
    return f"Пользователь {_name(user)} разблокирован."
