from __future__ import annotations

import asyncio
import html
import json
import os
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from loguru import logger
from telethon import TelegramClient, events
from telethon.tl.types import User
from telethon.utils import get_peer_id

from userbot.ai.brain import AIBrain
from userbot.ai.autoreply import AutoreplyContextStore
from userbot.ai.autoreply_worker import AutoreplyWorker
from userbot.constants import FTG_MODULES_PATH, NATIVE_MODULES_PATH
from userbot.core.loader import (
    configure,
    get_help_text,
    load_all_modules,
    load_ftg_module,
    load_native_module,
    loaded_module_count,
    module_config_text,
    module_status_text,
    reload_module,
    set_module_config,
    unload_module,
)
from userbot.core.bot_registration import ensure_control_bot, mark_setup_complete
from userbot.core.entities import resolve_entity
from userbot.core.file_library import FileLibrary
from userbot.core.instance_lock import SingleInstanceLock
from userbot.core.module_installer import ModuleInstaller
from userbot.inline.control_bot import ControlBotUnavailable, FaustControlBot
from userbot.core.utils import smart_split


USERBOT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = USERBOT_DIR / "config.json"
LOG_DIR = USERBOT_DIR / "logs"
SESSION_DIR = USERBOT_DIR / "sessions"
DATA_DIR = USERBOT_DIR / "data"
BOT_CONFIG_PATH = USERBOT_DIR / "botconfig.json"
for directory in (LOG_DIR, SESSION_DIR, DATA_DIR):
    directory.mkdir(parents=True, exist_ok=True)

logger.add(
    str(LOG_DIR / "userbot.log"),
    rotation="10 MB",
    retention="14 days",
    level="INFO",
    enqueue=True,
)


DEFAULT_CONFIG = {
    "API_ID": 0,
    "API_HASH": "",
    "prefix": ".",
    "device_model": "Faust Userbot",
    "system_version": "Ubuntu 24.04",
    "app_version": "3.5.1",
    "ollama_url": "http://87.120.84.247:11434",
    "model": "qwen3:1.7b",
    "ollama": {
        "timeout": 120,
        "max_queue": 20,
        "num_ctx": 2048,
        "num_predict": 256,
        "keep_alive": "30m",
        "history_turns": 6,
        "user_requests_per_minute": 10,
        "retry_attempts": 1,
        "circuit_threshold": 3,
        "circuit_seconds": 30,
    },
    "auto_answer_private_only": True,
    "autoreply_cooldown_seconds": 5.0,
    "autoreply_context_refresh_seconds": 86400,
    "autoreply_context_messages": 100,
    "autoreply_queue_size": 1000,
}


def deep_merge(default: dict, loaded: dict) -> dict:
    result = dict(default)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        raise RuntimeError(f"Заполни API_ID, API_HASH и ollama_url в {CONFIG_PATH}")
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Некорректный config.json: {exc}") from exc
    config = deep_merge(DEFAULT_CONFIG, loaded)
    if not int(config.get("API_ID") or 0) or not str(config.get("API_HASH") or "").strip():
        raise RuntimeError("В config.json не заполнены API_ID и API_HASH")
    if not str(config.get("ollama_url", "")).startswith(("http://", "https://")):
        raise RuntimeError("ollama_url должен начинаться с http:// или https://")
    return config


async def edit_long(event, text: str, *, include_original: str = "", parse_mode=None):
    value = f"{include_original}\n\n{text}" if include_original else str(text)
    parts = list(smart_split(value, 3900)) or ["Пустой ответ"]
    await event.edit(parts[0], parse_mode=parse_mode)
    for part in parts[1:]:
        await event.respond(part, parse_mode=parse_mode)


async def reply_long(message, text: str):
    sent = []
    for part in smart_split(str(text), 3900):
        sent.append(await message.reply(part, parse_mode=None))
    return sent


async def get_target_id(client, event, raw_target):
    if event.is_reply:
        reply = await event.get_reply_message()
        if reply and reply.sender_id:
            return int(reply.sender_id), None
        return None, "В сообщении реплая нет отправителя."
    if raw_target:
        raw_target = raw_target.strip()
        try:
            if re.fullmatch(r"-?\d+", raw_target):
                return int(raw_target), None
            entity = await client.get_entity(raw_target)
            return int(entity.id), None
        except Exception as exc:
            return None, f"Не удалось найти пользователя: {exc}"
    return None, None


def access_status(ai: AIBrain, section: str) -> str:
    value = ai.settings[section]
    lines = [f"{section}: для всех {'включено' if value['all'] else 'выключено'}"]
    if value["users"]:
        lines.append("ID пользователей: " + ", ".join(map(str, value["users"])))
    else:
        lines.append("Индивидуальный список пуст.")
    if section == "bypass":
        lines.append("Владелец всегда имеет доступ к AI-действиям.")
    return "\n".join(lines)


async def download_module_from_event(event, url: str | None) -> tuple[bytes, str, str]:
    reply = await event.get_reply_message()
    if reply and reply.file:
        name = reply.file.name or "module.py"
        if not name.endswith(".py"):
            raise ValueError("В реплае должен быть файл .py")
        content = await event.client.download_media(reply, file=bytes)
        return content, name, f"telegram:{reply.id}"
    if not url:
        raise ValueError("Ответь командой на .py файл или укажи HTTPS-ссылку")
    if not url.startswith("https://"):
        raise ValueError("Для загрузки модулей разрешены только HTTPS-ссылки")
    timeout = aiohttp.ClientTimeout(total=25, connect=7)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as response:
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}")
            if response.url.scheme != "https":
                raise ValueError("Перенаправление на небезопасный HTTP запрещено")
            content = await response.read()
    name = Path(urlparse(url).path).name or "module.py"
    return content, name, url


async def main():
    runtime_started = time.monotonic()
    config = load_config()
    prefix = str(config.get("prefix") or ".")
    ollama_cfg = config["ollama"]
    ai = AIBrain(
        ollama_url=config["ollama_url"],
        model=config["model"],
        base_dir=str(USERBOT_DIR / "ai"),
        timeout=ollama_cfg["timeout"],
        max_queue=ollama_cfg["max_queue"],
        num_ctx=ollama_cfg["num_ctx"],
        num_predict=ollama_cfg["num_predict"],
        keep_alive=ollama_cfg["keep_alive"],
        history_turns=ollama_cfg["history_turns"],
        user_requests_per_minute=ollama_cfg["user_requests_per_minute"],
        retry_attempts=ollama_cfg["retry_attempts"],
        circuit_threshold=ollama_cfg["circuit_threshold"],
        circuit_seconds=ollama_cfg["circuit_seconds"],
    )
    ai.file_library = FileLibrary(DATA_DIR / "files", DATA_DIR / "file_library.json")

    print(f"Проверяю Ollama {config['ollama_url']} и модель {config['model']}...")
    healthy, detail = await ai.health_check()
    if not healthy:
        logger.error(f"Проверка Ollama не пройдена: {detail}")
        print(f"Запуск остановлен: {detail}")
        await ai.close()
        raise RuntimeError(f"Ollama не прошла проверку: {detail}")
    print(detail)

    session_base = SESSION_DIR / "faust"
    client = TelegramClient(
        str(session_base),
        int(config["API_ID"]),
        str(config["API_HASH"]),
        device_model=str(config["device_model"]),
        system_version=str(config["system_version"]),
        app_version=str(config["app_version"]),
    )
    try:
        await client.start()
    except Exception:
        await ai.close()
        await client.disconnect()
        raise
    me = await client.get_me()
    ai.set_owner_id(me.id)
    context_store = AutoreplyContextStore(
        DATA_DIR / "autoreply_contexts",
        me.id,
        refresh_seconds=int(config["autoreply_context_refresh_seconds"]),
        limit=int(config["autoreply_context_messages"]),
        prefix=prefix,
    )
    session_file = session_base.with_suffix(".session")
    if session_file.exists():
        try:
            os.chmod(session_file, 0o600)
        except OSError:
            logger.warning("Не удалось выставить права 600 на файл сессии")

    control_config: dict = {}
    control_created = False
    control_registration_error = ""
    try:
        bot_config_raw = BOT_CONFIG_PATH.read_text(encoding="utf-8").strip() if BOT_CONFIG_PATH.exists() else ""
        if bot_config_raw in {"", "{}"}:
            print("Личный inline-бот не найден. Регистрирую через BotFather...")
        control_config, control_created = await ensure_control_bot(client, BOT_CONFIG_PATH)
    except Exception as exc:
        control_registration_error = str(exc)
        logger.exception("Не удалось подготовить личного inline-бота; доступен командный режим")

    configure(client, me.id, prefix, str(DATA_DIR / "ftg_db.json"))
    installer = ModuleInstaller(
        USERBOT_DIR,
        Path(FTG_MODULES_PATH),
        Path(NATIVE_MODULES_PATH),
    )
    await load_all_modules(client)
    logger.info(f"Юзербот запущен: {me.id}; модель: {config['model']}")
    control: FaustControlBot | None = None
    control_runtime_error = control_registration_error

    def runtime_text() -> str:
        total = int(time.monotonic() - runtime_started)
        days, remainder = divmod(total, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        username = str(control_config.get("username") or "не запущен").lstrip("@")
        return (
            f"Faust Tool: работает\n"
            f"Uptime: {days} д. {hours:02d}:{minutes:02d}:{seconds:02d}\n"
            f"Модель: {config['model']}\n"
            f"AI-действий: {len(ai.actions)}\n"
            f"Модулей: {loaded_module_count()}\n"
            f"Панель: @{username}"
        )

    async def panel_action(action: str, context_chat_id: int | None = None) -> str:
        if action == "status":
            return runtime_text()
        if action == "health":
            ok, result = await ai.health_check()
            return ("OK: " if ok else "Ошибка: ") + result
        if action == "ai":
            return await ai.stats_text()
        if action == "modules":
            return module_status_text()
        if action == "actions":
            return await ai.list_capabilities()
        if action == "files":
            return ai.file_library.list_text()
        if action == "autoreply":
            return ai.autoreply_status_text()
        if action in {"ar_status_all", "ar_status_chat"}:
            return ai.autoreply_scope_status(action.rsplit("_", 1)[-1], context_chat_id)
        if action == "reload":
            count = ai.reload()
            return f"AI-конфигурация перечитана. Действий: {count}"
        disable = re.fullmatch(r"ar_disable_(all|chat)", action)
        if disable:
            return ai.select_autoreply(None, disable.group(1), context_chat_id if disable.group(1) == "chat" else None)
        match = re.fullmatch(r"ar_(persona|assistant)_(on|off)_(all|chat)", action)
        if match:
            mode, state, scope = match.groups()
            target = None
            if scope == "chat":
                if context_chat_id is None:
                    raise ValueError(
                        "Текущий чат не определён. Открой панель командой .panel именно в нужном чате."
                    )
                target = context_chat_id
            if state == "on":
                return ai.select_autoreply(mode, scope, target)
            return ai.configure_autoreply(mode, scope, False, target)
        raise ValueError("Неизвестная команда панели")

    @client.on(
        events.NewMessage(
            outgoing=True,
            pattern=rf"^{re.escape(prefix)}help(?:\s+([A-Za-zА-Яа-яЁё]+))?$",
        )
    )
    async def help_handler(event):
        section = event.pattern_match.group(1)
        await edit_long(event, get_help_text(section), parse_mode="html")

    @client.on(
        events.NewMessage(
            outgoing=True,
            pattern=rf"^{re.escape(prefix)}(syshelp|aihelp|filehelp|modhelp|ftghelp|panelhelp)$",
        )
    )
    async def section_help_handler(event):
        command = str(event.pattern_match.group(1) or "")
        section = {
            "syshelp": "system",
            "aihelp": "ai",
            "filehelp": "files",
            "modhelp": "modules",
            "ftghelp": "ftg",
            "panelhelp": "panel",
        }[command]
        await edit_long(event, get_help_text(section), parse_mode="html")

    @client.on(
        events.NewMessage(
            outgoing=True,
            pattern=rf"^{re.escape(prefix)}panel(?:\s+([A-Za-zА-Яа-яЁё]+))?$",
        )
    )
    async def panel_handler(event):
        action = str(event.pattern_match.group(1) or "").strip().lower()
        if action in {"", "retry"}:
            if control is not None:
                try:
                    await control.open_inline(client, event)
                    return
                except ControlBotUnavailable as exc:
                    reason = str(exc) or "inline-вызов не поддерживается в этом чате"
                except Exception as exc:
                    reason = str(exc)
            else:
                reason = control_runtime_error or "личный inline-бот не запущен"
            fallback = (
                "<b>Inline-панель недоступна</b>\n"
                f"<blockquote>{html.escape(reason)}\n\n"
                "Faust Tool переключён в режим управления командами.</blockquote>\n\n"
                + get_help_text("panel")
            )
            await edit_long(event, fallback, parse_mode="html")
            return
        if action in {"help", "помощь"}:
            await edit_long(event, get_help_text("panel"), parse_mode="html")
            return
        aliases = {
            "статус": "status", "модули": "modules", "действия": "actions",
            "обновить": "reload", "файлы": "files", "автоответ": "autoreply",
        }
        action = aliases.get(action, action)
        if action not in {"status", "health", "ai", "modules", "actions", "reload", "files", "autoreply"}:
            await edit_long(event, get_help_text("panel"), parse_mode="html")
            return
        if action == "health":
            await event.edit("Проверяю Ollama...", parse_mode=None)
        try:
            result = await panel_action(action)
            text = f"<b>{html.escape(action.upper())}</b>\n<blockquote>{html.escape(result)}</blockquote>"
        except Exception as exc:
            text = f"<b>Ошибка панели</b>\n<blockquote>{html.escape(str(exc))}</blockquote>"
        await edit_long(event, text, parse_mode="html")

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}ping$"))
    async def ping_handler(event):
        started = time.monotonic()
        await event.edit("Проверка...", parse_mode=None)
        elapsed = (time.monotonic() - started) * 1000
        await event.edit(f"Faust работает: {elapsed:.1f} мс", parse_mode=None)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}uptime$"))
    async def uptime_handler(event):
        total = int(time.monotonic() - runtime_started)
        days, remainder = divmod(total, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        await event.edit(
            f"Uptime: {days} д. {hours:02d}:{minutes:02d}:{seconds:02d}",
            parse_mode=None,
        )

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}id$"))
    async def id_handler(event):
        lines = [f"Мой ID: {me.id}", f"ID чата: {event.chat_id}"]
        if event.is_reply:
            reply = await event.get_reply_message()
            if reply and reply.sender_id:
                lines.append(f"ID автора реплая: {reply.sender_id}")
            if reply:
                lines.append(f"ID сообщения: {reply.id}")
        await event.edit("\n".join(lines), parse_mode=None)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}(?:фауст|faust)(?:\s+([\s\S]+))?$"))
    async def faust_handler(event):
        original = event.raw_text
        text = (event.pattern_match.group(1) or "").strip()
        if not text:
            await event.edit(f"Пример: {prefix}фауст расскажи короткий факт", parse_mode=None)
            return
        await event.edit(
            "<b>Faust анализирует запрос</b>\n"
            f"<blockquote><code>{html.escape(text)}</code></blockquote>\n"
            "<i>Понимаю смысл, проверяю контекст и выбираю подходящее действие…</i>",
            parse_mode="html",
        )
        response = await ai.process_message(
            me.id,
            me.username or me.first_name or str(me.id),
            text,
            True,
            client=client,
            message=event.message,
            chat_id=event.chat_id,
        )
        await edit_long(event, response or "Ответ не получен.", include_original=original)

    async def access_handler(event, section: str):
        state = event.pattern_match.group(1)
        raw_target = event.pattern_match.group(2)
        if state is None:
            await event.edit(access_status(ai, section), parse_mode=None)
            return
        target_id, error = await get_target_id(client, event, raw_target)
        if error:
            await event.edit(error, parse_mode=None)
            return
        enabled = state == "on"
        if section == "answer":
            ai.set_answer(target_id, enabled)
        else:
            ai.set_bypass(target_id, enabled)
        scope = "для всех" if target_id is None else f"для ID {target_id}"
        await event.edit(f"{section} {scope}: {'включено' if enabled else 'выключено'}", parse_mode=None)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}answer(?:\s+(on|off))?(?:\s+(.+))?$"))
    async def answer_handler(event):
        await access_handler(event, "answer")

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}bypass(?:\s+(on|off))?(?:\s+(.+))?$"))
    async def bypass_handler(event):
        await access_handler(event, "bypass")

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}confirm(?:\s+([A-Fa-f0-9]+))?$"))
    async def confirm_handler(event):
        token = event.pattern_match.group(1) or ""
        if not token:
            await event.edit(f"Использование: {prefix}confirm КОД", parse_mode=None)
            return
        result = await ai.confirm_action(me.id, token, client=client, message=event.message)
        await edit_long(event, result)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}cancel$"))
    async def cancel_handler(event):
        count = ai.cancel_actions(me.id)
        await event.edit(f"Отменено действий: {count}", parse_mode=None)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}health$"))
    async def health_handler(event):
        await event.edit("Проверяю Ollama...", parse_mode=None)
        ok, result = await ai.health_check()
        await event.edit(("OK: " if ok else "Ошибка: ") + result, parse_mode=None)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}aistats$"))
    async def stats_handler(event):
        await event.edit(await ai.stats_text(), parse_mode=None)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}aiactions$"))
    async def actions_handler(event):
        await edit_long(event, await ai.list_capabilities())

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}aireload$"))
    async def ai_reload_handler(event):
        count = ai.reload()
        await event.edit(f"AI-конфигурация перечитана. Действий: {count}", parse_mode=None)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}forget$"))
    async def forget_handler(event):
        ai.forget(event.chat_id)
        await event.edit("История AI для этого чата очищена.", parse_mode=None)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}files$"))
    async def files_handler(event):
        await edit_long(event, ai.file_library.list_text())

    @client.on(
        events.NewMessage(
            outgoing=True,
            pattern=rf"^{re.escape(prefix)}autoreply(?:\s+(persona|assistant))?(?:\s+(on|off))?(?:\s+(all|user|chat|contacts|noncontacts))?(?:\s+(.+))?$",
        )
    )
    async def autoreply_handler(event):
        mode = event.pattern_match.group(1)
        state = event.pattern_match.group(2)
        scope = event.pattern_match.group(3)
        raw_target = (event.pattern_match.group(4) or "").strip()
        if not mode and not state and not scope:
            await edit_long(event, ai.autoreply_status_text())
            return
        if not (mode and state and scope):
            await event.edit(
                f"Использование: {prefix}autoreply persona|assistant on|off all|user|chat|contacts|noncontacts [ID]",
                parse_mode=None,
            )
            return
        target = raw_target or None
        try:
            if scope == "chat" and target is None:
                target = event.chat_id
            elif scope == "user" and target is None and event.is_reply:
                reply = await event.get_reply_message()
                target = getattr(reply, "sender_id", None)
            if scope in {"chat", "user"} and target is not None and not str(target).lstrip("-").isdigit():
                entity = await resolve_entity(client, target, message=event.message, user_only=scope == "user")
                target = entity.id if scope == "user" else get_peer_id(entity)
            result = ai.configure_autoreply(mode, scope, state == "on", target)
            await event.edit(result, parse_mode=None)
        except Exception as exc:
            await event.edit(f"Не удалось изменить автоответчик: {exc}", parse_mode=None)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}modules$"))
    async def modules_handler(event):
        await edit_long(event, module_status_text())

    @client.on(
        events.NewMessage(
            outgoing=True,
            pattern=rf"^{re.escape(prefix)}ftgconfig(?:\s+(\S+))?(?:\s+(\S+))?(?:\s+([\s\S]+))?$",
        )
    )
    async def ftg_config_handler(event):
        module_name = (event.pattern_match.group(1) or "").strip()
        key = (event.pattern_match.group(2) or "").strip()
        value = event.pattern_match.group(3)
        if not module_name:
            await event.edit(f"Использование: {prefix}ftgconfig МОДУЛЬ [КЛЮЧ] [ЗНАЧЕНИЕ]", parse_mode=None)
            return
        try:
            if not key or value is None:
                await edit_long(event, module_config_text(module_name))
                return
            saved = set_module_config(module_name, key, value.strip())
            await event.edit(f"{module_name}.{key} = {saved!r}", parse_mode=None)
        except Exception as exc:
            await event.edit(f"Конфиг не изменён: {exc}", parse_mode=None)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}reloadmod(?:\s+(.+))?$"))
    async def reload_handler(event):
        name = (event.pattern_match.group(1) or "").strip()
        if not name:
            await event.edit(f"Использование: {prefix}reloadmod имя", parse_mode=None)
            return
        try:
            await reload_module(name)
            await event.edit(f"Модуль {name} перезагружен.", parse_mode=None)
        except Exception as exc:
            await event.edit(f"Ошибка перезагрузки: {exc}", parse_mode=None)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}unloadmod(?:\s+(.+))?$"))
    async def unload_handler(event):
        name = (event.pattern_match.group(1) or "").strip()
        if not name:
            await event.edit(f"Использование: {prefix}unloadmod имя", parse_mode=None)
            return
        removed = await unload_module(name)
        await event.edit("Модуль выгружен." if removed else "Модуль не найден.", parse_mode=None)

    async def stage_module(event, kind: str):
        url = event.pattern_match.group(1)
        try:
            content, filename, source = await download_module_from_event(event, url)
            item = installer.stage(content, filename, kind, source)
            await edit_long(event, installer.describe(item))
        except Exception as exc:
            await event.edit(f"Модуль не подготовлен: {exc}", parse_mode=None)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}dlmod(?:\s+(\S+))?$"))
    async def dlmod_handler(event):
        await stage_module(event, "ftg")

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}dlnative(?:\s+(\S+))?$"))
    async def dlnative_handler(event):
        await stage_module(event, "native")

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}modconfirm(?:\s+([A-Fa-f0-9]+))?$"))
    async def modconfirm_handler(event):
        token = event.pattern_match.group(1) or ""
        try:
            item = installer.confirm(token)
            if item.kind == "ftg":
                await load_ftg_module(item.path.stem, client)
            else:
                await load_native_module(str(item.path), client)
            await event.edit(f"Модуль {item.filename} установлен и загружен.", parse_mode=None)
        except Exception as exc:
            await event.edit(f"Установка не выполнена: {exc}", parse_mode=None)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{re.escape(prefix)}modcancel$"))
    async def modcancel_handler(event):
        count = installer.cancel_all()
        await event.edit(f"Отменено установок: {count}", parse_mode=None)

    recent_ids = deque(maxlen=2000)
    autoreply_worker = AutoreplyWorker(
        client, ai, context_store,
        cooldown=max(5.0, float(config["autoreply_cooldown_seconds"])),
        queue_size=int(config["autoreply_queue_size"]),
        legacy_private_only=bool(config["auto_answer_private_only"]),
    )
    autoreply_task = asyncio.create_task(autoreply_worker.run(), name="faust-autoreply-worker")

    @client.on(events.NewMessage(incoming=True))
    async def incoming_handler(event):
        message = event.message
        message_key = (message.chat_id, message.id)
        if message_key in recent_ids:
            return
        recent_ids.append(message_key)
        sender = await event.get_sender()
        if not isinstance(sender, User) or sender.bot or sender.id == me.id:
            return
        text = (message.raw_text or "").strip()
        if not text:
            return

        command_match = re.match(
            rf"^{re.escape(prefix)}(?:фауст|faust)(?:\s+([\s\S]+))?$",
            text,
            re.I,
        )
        if command_match:
            if not ai.can_bypass(sender.id):
                await message.reply("Нет прав на AI-действия.", parse_mode=None)
                return
            command_text = (command_match.group(1) or "").strip()
            async with client.action(message.chat_id, "typing"):
                response = await ai.process_message(
                    sender.id,
                    sender.username or sender.first_name or str(sender.id),
                    command_text,
                    True,
                    client=client,
                    message=message,
                    chat_id=message.chat_id,
                )
            if response:
                for sent in await reply_long(message, response):
                    context_store.append(sent, generated_by_ai=True)
            return
        context_store.append(message)
        autoreply_worker.enqueue(message, sender, is_private=bool(event.is_private))

    @client.on(events.NewMessage(outgoing=True))
    async def outgoing_context_handler(event):
        text = (event.raw_text or "").strip()
        if text.startswith(prefix):
            return
        context_store.append(event.message)

    if control_config.get("token") and control_config.get("username"):
        try:
            control = FaustControlBot(
                api_id=int(config["API_ID"]),
                api_hash=str(config["API_HASH"]),
                token=str(control_config["token"]),
                username=str(control_config["username"]),
                owner_id=me.id,
                session_path=SESSION_DIR / "faust_control_bot",
                prefix=prefix,
                action_handler=panel_action,
            )
            await control.start()
            control_runtime_error = ""
            if control_created or not bool(control_config.get("setup_complete")):
                await control.send_first_start(client)
                mark_setup_complete(BOT_CONFIG_PATH, control_config)
                control_config["setup_complete"] = True
        except Exception as exc:
            control_runtime_error = str(exc)
            logger.exception("Личный inline-бот не запущен; доступен командный режим")
            if control is not None:
                await control.stop()
            control = None

    print(f"Юзербот готов: {me.first_name or me.id}")
    print(f"Команды начинаются с {prefix}")
    print(f"Справка: {prefix}help")
    try:
        await client.run_until_disconnected()
    finally:
        autoreply_task.cancel()
        await asyncio.gather(autoreply_task, return_exceptions=True)
        installer.cancel_all()
        if control is not None:
            await control.stop()
        await ai.close()
        await client.disconnect()


if __name__ == "__main__":
    try:
        with SingleInstanceLock(DATA_DIR / "userbot.lock"):
            asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Критическая ошибка запуска")
        raise
