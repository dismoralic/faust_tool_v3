from __future__ import annotations

from typing import Any

from telethon import functions
from telethon.tl.types import InputFolderPeer

from userbot.ai.brain import ai_function
from userbot.core.entities import resolve_entity


BLOCK_INFO = {
    "name": "dialog_manager",
    "description": "Список, прочтение и архивирование диалогов",
}


async def _entity(client: Any, message: Any, target: Any):
    return await resolve_entity(client, target, message=message)


def _name(entity: Any) -> str:
    return (
        getattr(entity, "title", None)
        or " ".join(
            value
            for value in (getattr(entity, "first_name", None), getattr(entity, "last_name", None))
            if value
        )
        or getattr(entity, "username", None)
        or str(getattr(entity, "id", "неизвестно"))
    )


@ai_function(
    description="Показывает последние Telegram-диалоги и число непрочитанных сообщений",
    triggers=["покажи список диалогов", "покажи диалоги", "последние диалоги"],
    params=[{"name": "limit", "type": "integer", "default": 10, "min": 1, "max": 30}],
    risk="read",
    name="list_dialogs",
)
async def list_dialogs(limit: int = 10, client=None, message=None):
    lines = []
    async for dialog in client.iter_dialogs(limit=limit):
        unread = int(getattr(dialog, "unread_count", 0) or 0)
        lines.append(f"{getattr(dialog.entity, 'id', '?')} | {_name(dialog.entity)} | непрочитано: {unread}")
    return "Диалоги:\n" + "\n".join(lines) if lines else "Диалогов не найдено."


@ai_function(
    description="Показывает диалоги, в которых есть непрочитанные сообщения",
    triggers=["покажи непрочитанные диалоги", "где есть непрочитанные", "непрочитанные чаты"],
    params=[{"name": "limit", "type": "integer", "default": 10, "min": 1, "max": 30}],
    risk="read",
    name="unread_dialogs",
)
async def unread_dialogs(limit: int = 10, client=None, message=None):
    lines = []
    async for dialog in client.iter_dialogs():
        unread = int(getattr(dialog, "unread_count", 0) or 0)
        if unread:
            lines.append(f"{getattr(dialog.entity, 'id', '?')} | {_name(dialog.entity)} | {unread}")
        if len(lines) >= limit:
            break
    return "Непрочитанные диалоги:\n" + "\n".join(lines) if lines else "Непрочитанных диалогов нет."


@ai_function(
    description="Помечает выбранный диалог как прочитанный",
    triggers=["пометь чат прочитанным", "прочитай диалог", "отметь как прочитанное"],
    params=[{"name": "target", "type": "string", "description": "@username или ID"}],
    risk="write",
    owner_only=True,
    name="mark_dialog_read",
)
async def mark_dialog_read(target: str = None, client=None, message=None):
    entity = await _entity(client, message, target)
    await client.send_read_acknowledge(entity)
    return f"Диалог {_name(entity)} помечен как прочитанный."


async def _set_folder(folder_id: int, target: str, client, message):
    entity = await _entity(client, message, target)
    peer = await client.get_input_entity(entity)
    await client(functions.folders.EditPeerFoldersRequest([InputFolderPeer(peer=peer, folder_id=folder_id)]))
    return entity


@ai_function(
    description="Перемещает выбранный диалог в архив",
    triggers=["архивируй диалог", "убери чат в архив", "перемести диалог в архив"],
    params=[{"name": "target", "type": "string", "description": "@username или ID"}],
    risk="write",
    confirmation=True,
    owner_only=True,
    name="archive_dialog",
)
async def archive_dialog(target: str = None, client=None, message=None):
    entity = await _set_folder(1, target, client, message)
    return f"Диалог {_name(entity)} перемещён в архив."


@ai_function(
    description="Возвращает выбранный диалог из архива",
    triggers=["достань диалог из архива", "верни чат из архива", "разархивируй диалог"],
    params=[{"name": "target", "type": "string", "description": "@username или ID"}],
    risk="write",
    confirmation=True,
    owner_only=True,
    name="unarchive_dialog",
)
async def unarchive_dialog(target: str = None, client=None, message=None):
    entity = await _set_folder(0, target, client, message)
    return f"Диалог {_name(entity)} возвращён из архива."
