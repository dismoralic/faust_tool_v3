from __future__ import annotations

import asyncio
import hashlib
import html
import inspect
import os
import re
import shlex
from pathlib import Path
from typing import Iterable, List, Optional, Union

from telethon.tl.custom.message import Message


def get_args_raw(message: Union[Message, str]) -> str:
    text = message if isinstance(message, str) else (message.raw_text or "")
    return text.split(maxsplit=1)[1] if " " in text else ""


def get_args(message: Union[Message, str]) -> List[str]:
    raw = get_args_raw(message)
    return raw.split() if raw else []


def get_args_split_by(message: Union[Message, str], separator: Optional[str] = None) -> List[str]:
    raw = get_args_raw(message)
    if not raw:
        return []
    return [item.strip() for item in raw.split(separator) if item.strip()] if separator else raw.split()


def get_args_html(message: Union[Message, str]) -> str:
    return escape_html(get_args_raw(message))


def get_chat_id(message: Message) -> int:
    return message.chat_id


async def get_user(message: Message):
    if message.is_reply:
        reply = await message.get_reply_message()
        if reply:
            return await reply.get_sender()
    return await message.get_sender()


async def answer(message: Message, text: str = None, **kwargs):
    if "reply_markup" in kwargs and "buttons" not in kwargs:
        kwargs["buttons"] = kwargs.pop("reply_markup")
    if "parse_mode" not in kwargs:
        kwargs["parse_mode"] = "html"
    if message.out:
        return await message.edit(text, **kwargs)
    return await message.reply(text, **kwargs)


async def answer_file(message: Message, file_path: str, **kwargs):
    kwargs.setdefault("parse_mode", "html")
    if message.out:
        return await message.respond(file=file_path, **kwargs)
    return await message.reply(file=file_path, **kwargs)


def get_display_name(entity) -> str:
    first = getattr(entity, "first_name", None)
    last = getattr(entity, "last_name", None)
    title = getattr(entity, "title", None)
    username = getattr(entity, "username", None)
    return " ".join(item for item in (first, last) if item) or title or username or str(getattr(entity, "id", "Unknown"))


def get_formatted_text(text: str, **kwargs) -> str:
    return text.format(**kwargs)


def escape_html(text) -> str:
    return html.escape(str(text), quote=False)


def escape_markdown(text: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(text))


def remove_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", str(text)))


def get_kwargs(message: Union[Message, str]) -> dict:
    """Разбирает аргументы вида key=value с учётом кавычек."""
    result = {}
    for token in shlex.split(get_args_raw(message)):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key:
            result[key] = value
    return result


def get_file_md5(file_path: str) -> str:
    digest = hashlib.md5()
    with open(file_path, "rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_size_readable(size_bytes: int) -> str:
    value = float(size_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


async def run_sync(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def clean_text(text: str) -> str:
    return " ".join(str(text).split())


def remove_emoji(text: str) -> str:
    return re.sub(r"[\U00010000-\U0010ffff]", "", str(text))


def chunks(items: Iterable, size: int):
    items = list(items)
    for index in range(0, len(items), size):
        yield items[index:index + size]


def smart_split(text: str, length: int = 4096, split_on=("\n", " ")):
    text = str(text)
    while len(text) > length:
        position = max(text.rfind(separator, 0, length) for separator in split_on)
        if position <= 0:
            position = length
        yield text[:position]
        text = text[position:].lstrip()
    if text:
        yield text


def get_named_platform() -> str:
    return "Faust Userbot"


def get_platform() -> str:
    return "faust"


def get_lang_flag(country_code: str) -> str:
    code = str(country_code or "").upper()[:2]
    if len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(127397 + ord(char)) for char in code)


def check_url(url: str) -> bool:
    return bool(re.fullmatch(r"https?://[^\s]+", str(url)))


def get_entity_url(entity) -> Optional[str]:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}"
    entity_id = getattr(entity, "id", None)
    return f"tg://user?id={entity_id}" if entity_id else None


async def get_message_link(message: Message) -> Optional[str]:
    chat = await message.get_chat()
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message.id}"
    chat_id = str(abs(message.chat_id))
    if chat_id.startswith("100"):
        chat_id = chat_id[3:]
    return f"https://t.me/c/{chat_id}/{message.id}" if chat_id else None


def get_topic(message: Message) -> Optional[int]:
    reply_to = getattr(message, "reply_to", None)
    return getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None)


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


async def asset_channel(client, title: str, description: str = "", **kwargs):
    async for dialog in client.iter_dialogs():
        if getattr(dialog, "title", None) == title:
            return dialog.entity, False
    result = await client.create_channel(title, description, megagroup=kwargs.get("megagroup", False))
    return result.chats[0], True
