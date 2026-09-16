from __future__ import annotations

from datetime import datetime, timedelta, timezone

from userbot.ai.brain import ai_function
from userbot.core.entities import entity_name, resolve_entity


async def _reply(message):
    if message is None or not getattr(message, "is_reply", False):
        raise ValueError("Ответь командой на нужное сообщение")
    reply = await message.get_reply_message()
    if reply is None:
        raise ValueError("Сообщение в реплае не найдено")
    return reply


def _history_text(messages, owner_id=None, max_chars=6500):
    lines = []
    for item in reversed(messages):
        sender = "я" if owner_id and item.sender_id == owner_id else str(item.sender_id or "?")
        text = " ".join((item.raw_text or "[медиа]").split())
        lines.append(f"{sender}: {text[:900]}")
    return "\n".join(lines)[-max_chars:]


@ai_function(
    description="Анализирует до 100 последних сообщений и кратко пересказывает суть чата",
    triggers=["проанализируй этот чат", "сделай сводку чата", "кратко перескажи чат", "что происходит в чате"],
    params=[
        {"name": "target", "type": "string", "description": "ID, @username или имя чата"},
        {"name": "limit", "type": "integer", "default": 100, "min": 10, "max": 100},
    ],
    risk="read",
    name="summarize_chat",
)
async def summarize_chat(target: str = None, limit: int = 100, client=None, message=None, brain=None):
    entity = await resolve_entity(client, target, message=message)
    me = await client.get_me()
    items = [item async for item in client.iter_messages(entity, limit=limit)]
    history = _history_text(items, me.id)
    return await brain.generate_text(
        f"Чат {entity_name(entity)}:\n{history}\n\nДай короткую сводку: темы, решения, открытые вопросы.",
        system="Анализируй только предоставленные сообщения. Не выдумывай факты. Ответь по-русски.",
        num_predict=300,
        num_ctx=2048,
    )


@ai_function(
    description="Пишет черновик ответа на сообщение в реплае, ничего не отправляя",
    triggers=["составь ответ", "напиши черновик ответа", "предложи ответ"],
    params=[{"name": "text", "type": "string", "description": "пожелания к ответу", "max_length": 1000}],
    risk="read",
    name="draft_reply",
)
async def draft_reply(text: str = "", message=None, brain=None):
    reply = await _reply(message)
    return await brain.generate_text(
        f"Сообщение: {reply.raw_text or '[медиа]'}\nПожелания: {text or 'ответь уместно и кратко'}",
        system="Напиши только готовый черновик ответа на русском, без кавычек и пояснений.",
        num_predict=180,
    )


@ai_function(
    description="Генерирует и отправляет ответ на сообщение в реплае",
    triggers=["ответь на это сообщение", "напиши и отправь ответ"],
    params=[{"name": "text", "type": "string", "description": "пожелания к ответу", "max_length": 1000}],
    risk="write",
    confirmation=True,
    owner_only=True,
    name="generate_and_send_reply",
)
async def generate_and_send_reply(text: str = "", client=None, message=None, brain=None):
    reply = await _reply(message)
    result = await brain.generate_text(
        f"Сообщение: {reply.raw_text or '[медиа]'}\nПожелания: {text or 'ответь уместно и кратко'}",
        system="Напиши только готовый ответ на русском, без кавычек и пояснений.",
        num_predict=180,
    )
    sent = await client.send_message(message.chat_id, result, reply_to=reply.id, parse_mode=None)
    return f"Сгенерировал и отправил ответ. ID сообщения: {sent.id}"


@ai_function(
    description="Переводит сообщение из реплая на указанный язык",
    triggers=["переведи это сообщение", "переведи сообщение"],
    params=[{"name": "language", "type": "string", "default": "русский", "max_length": 40}],
    risk="read",
    name="translate_replied_message",
)
async def translate_replied_message(language: str = "русский", message=None, brain=None):
    reply = await _reply(message)
    return await brain.generate_text(
        reply.raw_text or "[медиа без текста]",
        system=f"Точно переведи текст на {language}. Верни только перевод.",
        num_predict=300,
    )


@ai_function(
    description="Объясняет простыми словами сообщение из реплая",
    triggers=["объясни это сообщение", "что значит это сообщение", "разбери сообщение"],
    risk="read",
    name="explain_replied_message",
)
async def explain_replied_message(message=None, brain=None):
    reply = await _reply(message)
    return await brain.generate_text(
        reply.raw_text or "[медиа без текста]",
        system="Объясни содержание простыми словами по-русски. Отделяй факты от предположений.",
        num_predict=260,
    )


@ai_function(
    description="Выделяет задачи и договорённости из последних сообщений чата",
    triggers=["выдели задачи из чата", "найди договоренности", "что нужно сделать в чате"],
    params=[{"name": "limit", "type": "integer", "default": 100, "min": 10, "max": 100}],
    risk="read",
    name="extract_chat_tasks",
)
async def extract_chat_tasks(limit: int = 100, client=None, message=None, brain=None):
    items = [item async for item in client.iter_messages(message.chat_id, limit=limit)]
    history = _history_text(items)
    return await brain.generate_text(
        history,
        system="Найди только реальные задачи, обещания, сроки и ответственных. Если их нет, так и скажи. По-русски.",
        num_predict=300,
        num_ctx=2048,
    )


@ai_function(
    description="Ищет текст сразу в нескольких последних Telegram-диалогах",
    triggers=["найди во всех чатах", "глобальный поиск сообщений", "поищи по диалогам"],
    params=[
        {"name": "query", "type": "string", "required": True, "max_length": 200},
        {"name": "limit", "type": "integer", "default": 20, "min": 1, "max": 50},
    ],
    risk="read",
    name="global_message_search",
)
async def global_message_search(query: str, limit: int = 20, client=None, message=None):
    lines = []
    async for dialog in client.iter_dialogs(limit=30):
        async for item in client.iter_messages(dialog.entity, search=query, limit=3):
            text = " ".join((item.raw_text or "[медиа]").split())[:180]
            lines.append(f"{entity_name(dialog.entity)} (ID {dialog.entity.id}), msg {item.id}: {text}")
            if len(lines) >= limit:
                break
        if len(lines) >= limit:
            break
    return "Найдено:\n" + "\n".join(lines) if lines else f"По запросу «{query}» ничего не найдено."


@ai_function(
    description="Сохраняет сообщение из реплая в Избранное Telegram",
    triggers=["сохрани это в избранное", "отправь это в сохраненные", "сохрани сообщение"],
    risk="write",
    owner_only=True,
    name="save_replied_to_saved_messages",
)
async def save_replied_to_saved_messages(client=None, message=None):
    reply = await _reply(message)
    sent = await client.forward_messages("me", reply)
    return f"Сохранил сообщение в Избранное. ID: {getattr(sent, 'id', '?')}"


@ai_function(
    description="Планирует отправку сообщения через указанное число минут",
    triggers=["запланируй сообщение", "отправь сообщение через", "напомни сообщением через"],
    params=[
        {"name": "text", "type": "string", "required": True, "max_length": 3500},
        {"name": "target", "type": "string", "description": "ID, @username или имя"},
        {"name": "delay_minutes", "type": "integer", "required": True, "min": 1, "max": 10080},
    ],
    risk="write",
    confirmation=True,
    owner_only=True,
    name="schedule_message",
)
async def schedule_message(text: str, delay_minutes: int, target: str = None, client=None, message=None):
    entity = await resolve_entity(client, target, message=message)
    when = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
    sent = await client.send_message(entity, text, schedule=when, parse_mode=None)
    return f"Запланировал сообщение в {entity_name(entity)} через {delay_minutes} мин. ID: {sent.id}"
