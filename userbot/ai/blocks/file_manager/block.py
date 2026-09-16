from __future__ import annotations

from userbot.ai.brain import ai_function
from userbot.core.entities import entity_name, resolve_entity


def _library(brain):
    library = getattr(brain, "file_library", None)
    if library is None:
        raise RuntimeError("Файловое хранилище ещё не подключено")
    return library


@ai_function(
    description="Скачивает медиа или файл из сообщения в реплае и присваивает ему ярлык",
    triggers=[
        "скачай этот файл", "сохрани этот файл", "сохрани его", "сохрани это",
        "сохрани медиа", "запомни файл", "запомни его под названием",
    ],
    params=[{"name": "alias", "type": "string", "required": True, "max_length": 80}],
    risk="write",
    owner_only=True,
    name="download_replied_file",
)
async def download_replied_file(alias: str, client=None, message=None, brain=None):
    item = await _library(brain).download_replied(client, message, alias)
    return f"Скачал {item['filename']} и запомнил под ярлыком «{item['alias']}»."


@ai_function(
    description="Отправляет ранее сохранённый файл по ярлыку пользователю или в чат",
    triggers=["скинь сохраненный файл", "отправь сохраненный файл", "скинь файл", "отправь файл по ярлыку"],
    params=[
        {"name": "alias", "type": "string", "required": True, "max_length": 80},
        {"name": "target", "type": "string", "description": "ID, @username или имя из диалогов"},
    ],
    risk="write",
    confirmation=True,
    owner_only=True,
    name="send_saved_file",
)
async def send_saved_file(alias: str, target: str = None, client=None, message=None, brain=None):
    path, item = _library(brain).resolve(alias)
    entity = await resolve_entity(client, target, message=message)
    sent = await client.send_file(entity, str(path), caption=f"Файл: {item['alias']}")
    return f"Отправил файл «{item['alias']}» в {entity_name(entity)}. ID сообщения: {sent.id}"


@ai_function(
    description="Показывает список файлов и их ярлыков в хранилище юзербота",
    triggers=["покажи сохраненные файлы", "список файлов", "какие файлы сохранены"],
    risk="read",
    name="list_saved_files",
)
async def list_saved_files(brain=None):
    return _library(brain).list_text()


@ai_function(
    description="Удаляет сохранённый файл и его ярлык с диска",
    triggers=["удали сохраненный файл", "забудь файл", "удали файл по ярлыку"],
    params=[{"name": "alias", "type": "string", "required": True, "max_length": 80}],
    risk="destructive",
    owner_only=True,
    name="delete_saved_file",
)
async def delete_saved_file(alias: str, brain=None):
    filename = _library(brain).delete(alias)
    return f"Удалил сохранённый файл {filename} и освободил ярлык «{alias}»."
