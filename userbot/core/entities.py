from __future__ import annotations

import re
from typing import Any, Optional


def entity_name(entity: Any) -> str:
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


def _normalize(value: Any) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "", str(value or "").lower())


async def resolve_entity(
    client: Any,
    target: Optional[Any] = None,
    *,
    message: Any = None,
    user_only: bool = False,
    dialog_limit: int = 250,
):
    """Resolve a Telegram entity by ID, username, current chat or visible dialog name."""
    if target not in (None, ""):
        if isinstance(target, int) or re.fullmatch(r"-?\d+", str(target).strip()):
            wanted_id = int(target)
            try:
                return await client.get_entity(wanted_id)
            except Exception as direct_error:
                async for dialog in client.iter_dialogs(limit=dialog_limit):
                    dialog_id = int(getattr(dialog, "id", 0) or 0)
                    entity_id = int(getattr(dialog.entity, "id", 0) or 0)
                    if wanted_id in {dialog_id, entity_id}:
                        return dialog.entity
                raise ValueError(f"Не нашёл Telegram ID {wanted_id} среди доступных диалогов") from direct_error
        raw = str(target).strip()
        if raw.startswith("@"):
            try:
                return await client.get_entity(raw)
            except Exception:
                raw = raw[1:]

        wanted = _normalize(raw)
        exact = []
        prefix = []
        async for dialog in client.iter_dialogs(limit=dialog_limit):
            entity = dialog.entity
            if user_only and not hasattr(entity, "first_name"):
                continue
            values = {
                _normalize(entity_name(entity)),
                _normalize(getattr(entity, "username", None)),
                _normalize(getattr(entity, "first_name", None)),
            }
            values.discard("")
            if wanted in values:
                exact.append(entity)
                continue
            stem = min(5, len(wanted))
            if stem >= 4 and any(value[:stem] == wanted[:stem] for value in values):
                prefix.append(entity)
        matches = exact or prefix
        unique = {int(entity.id): entity for entity in matches}
        if len(unique) == 1:
            return next(iter(unique.values()))
        if len(unique) > 1:
            options = ", ".join(
                f"{entity_name(entity)} (ID {entity.id})"
                for entity in list(unique.values())[:6]
            )
            raise ValueError(f"Имя неоднозначно. Укажи ID: {options}")
        raise ValueError(f"Не нашёл «{raw}» среди последних {dialog_limit} диалогов")

    if message is not None:
        get_chat = getattr(message, "get_chat", None)
        if callable(get_chat):
            entity = await get_chat()
            if entity is not None:
                return entity
        if getattr(message, "chat_id", None) is not None:
            return await client.get_entity(message.chat_id)
    raise ValueError("Укажи @username, Telegram ID, имя из диалогов или вызови команду в нужном чате")
