from __future__ import annotations

from typing import Any

from userbot.ai.brain import ai_function
from userbot.core.entities import resolve_entity


BLOCK_INFO = {
    "name": "message_manager",
    "description": "Пересылка, закрепление и удаление сообщений по реплаю",
}


async def _reply(message: Any):
    if message is None or not getattr(message, "is_reply", False):
        raise ValueError("Команда должна быть ответом на сообщение")
    reply = await message.get_reply_message()
    if reply is None:
        raise ValueError("Сообщение в реплае не найдено")
    return reply


async def _target(client: Any, message: Any, target: Any):
    return await resolve_entity(client, target, message=message)


def _name(entity: Any) -> str:
    return (
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or getattr(entity, "first_name", None)
        or str(getattr(entity, "id", "неизвестно"))
    )


@ai_function(
    description="Пересылает сообщение из реплая в выбранный чат",
    triggers=["перешли это сообщение", "перешли сообщение", "сделай форвард сообщения"],
    params=[{"name": "target", "type": "string", "description": "@username или ID"}],
    risk="write",
    confirmation=True,
    owner_only=True,
    name="forward_replied_message",
)
async def forward_replied_message(target: str = None, client=None, message=None):
    reply = await _reply(message)
    entity = await _target(client, message, target)
    sent = await client.forward_messages(entity, reply)
    sent_id = getattr(sent, "id", None) or "?"
    return f"Сообщение переслано в {_name(entity)}. ID: {sent_id}"


@ai_function(
    description="Закрепляет сообщение из реплая в текущем чате без уведомления",
    triggers=["закрепи это сообщение", "закрепи сообщение", "поставь сообщение в закреп"],
    params=[],
    risk="write",
    confirmation=True,
    owner_only=True,
    name="pin_replied_message",
)
async def pin_replied_message(client=None, message=None):
    reply = await _reply(message)
    await client.pin_message(message.chat_id, reply, notify=False)
    return f"Сообщение {reply.id} закреплено."


@ai_function(
    description="Открепляет сообщение из реплая в текущем чате",
    triggers=["открепи это сообщение", "открепи сообщение", "убери сообщение из закрепа"],
    params=[],
    risk="write",
    confirmation=True,
    owner_only=True,
    name="unpin_replied_message",
)
async def unpin_replied_message(client=None, message=None):
    reply = await _reply(message)
    await client.unpin_message(message.chat_id, reply)
    return f"Сообщение {reply.id} откреплено."


@ai_function(
    description="Удаляет сообщение из реплая, только если оно отправлено владельцем юзербота",
    triggers=["удали это мое сообщение", "удали мое сообщение в реплае"],
    params=[],
    risk="destructive",
    owner_only=True,
    name="delete_replied_own_message",
)
async def delete_replied_own_message(client=None, message=None):
    reply = await _reply(message)
    me = await client.get_me()
    if reply.sender_id != me.id:
        raise ValueError("Разрешено удалять только собственные сообщения")
    await client.delete_messages(message.chat_id, [reply.id], revoke=True)
    return f"Моё сообщение {reply.id} удалено."
