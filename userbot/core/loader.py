from __future__ import annotations

import asyncio
import html
import hashlib
import importlib.util
import inspect
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from telethon import events

from userbot.constants import FTG_MODULES_PATH, NATIVE_MODULES_PATH
from userbot.core.ftg_compat import Module, ModuleConfig, SelfSuspend, SelfUnload, StringsProxy
from userbot.core.module_installer import ModuleInstaller
from userbot.inline.types import InlineCall


@dataclass
class HandlerRef:
    callback: Any
    builder: Any


@dataclass
class ModuleRecord:
    key: str
    name: str
    kind: str
    instance: Any
    module: Any
    path: Path
    handlers: list[HandlerRef] = field(default_factory=list)
    tasks: list[asyncio.Task] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


class PersistentList(list):
    def __init__(self, values, save):
        super().__init__(values)
        self._save_callback = save

    def _save(self):
        self._save_callback(list(self))

    def append(self, value):
        super().append(value)
        self._save()

    def extend(self, values):
        super().extend(values)
        self._save()

    def insert(self, index, value):
        super().insert(index, value)
        self._save()

    def remove(self, value):
        super().remove(value)
        self._save()

    def pop(self, index=-1):
        value = super().pop(index)
        self._save()
        return value

    def clear(self):
        super().clear()
        self._save()

    def sort(self, *args, **kwargs):
        super().sort(*args, **kwargs)
        self._save()

    def reverse(self):
        super().reverse()
        self._save()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._save()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._save()

    def __iadd__(self, values):
        result = super().__iadd__(values)
        self._save()
        return result


class PersistentDict(dict):
    def __init__(self, values, save):
        super().__init__(values)
        self._save_callback = save

    def _save(self):
        self._save_callback(dict(self))

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._save()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._save()

    def clear(self):
        super().clear()
        self._save()

    def pop(self, key, *args):
        value = super().pop(key, *args)
        self._save()
        return value

    def popitem(self):
        value = super().popitem()
        self._save()
        return value

    def setdefault(self, key, default=None):
        exists = key in self
        value = super().setdefault(key, default)
        if not exists:
            self._save()
        return value

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self._save()


class PersistentDB:
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[str(key)] = value
        self._save()

    def __contains__(self, key):
        return str(key) in self.data

    def get(self, module, key=None, default=None):
        if key is None:
            return self.data.get(str(module), default)
        return self.data.get(str(module), {}).get(str(key), default)

    def set(self, module, key, value):
        self.data.setdefault(str(module), {})[str(key)] = value
        self._save()
        return value

    def pointer(self, module, key, default=None):
        value = self.get(module, key, None)
        if value is None:
            value = default
            self.set(module, key, value)
        save = lambda updated: self.set(module, key, updated)
        if isinstance(value, list):
            return PersistentList(value, save)
        if isinstance(value, dict):
            return PersistentDict(value, save)
        return value


class InlineStub:
    async def form(self, text, message=None, **kwargs):
        if message is None:
            return False
        if "reply_markup" in kwargs and "buttons" not in kwargs:
            kwargs["buttons"] = kwargs.pop("reply_markup")
        if getattr(message, "out", False):
            return await message.edit(text, parse_mode="html", **kwargs)
        return await message.reply(text, parse_mode="html", **kwargs)

    async def list(self, message=None, strings=None, **kwargs):
        text = "\n\n".join(strings or [])
        return await self.form(text, message=message, **kwargs)


class AllModules:
    def __init__(self):
        self.modules: list[Any] = []
        self.commands: dict[str, Any] = {}
        self.watchers: list[Any] = []

    def lookup(self, name: str):
        normalized = str(name).lower()
        for module in self.modules:
            module_name = str(getattr(module, "name", "")).lower()
            class_name = module.__class__.__name__.lower()
            if normalized in {module_name, class_name}:
                return module
        return None

    async def invoke(self, command_name: str, args: str = "", peer=None):
        if CLIENT is None:
            raise RuntimeError("Telegram-клиент не настроен")
        peer = peer or "me"
        text = f"{PREFIX}{command_name}" + (f" {args}" if args else "")
        return await CLIENT.send_message(peer, text, parse_mode=None)


CLIENT = None
OWNER_ID: Optional[int] = None
PREFIX = "."
DB: Optional[PersistentDB] = None
ALLMODULES = AllModules()
LOADED_MODULES: dict[str, ModuleRecord] = {}
LOADED_HANDLERS: dict[str, Any] = {}
RESERVED_COMMANDS = {
    "help", "syshelp", "aihelp", "filehelp", "modhelp", "ftghelp", "panelhelp",
    "ping", "uptime", "id", "faust", "фауст", "answer", "bypass", "panel",
    "confirm", "cancel", "health", "aistats", "aiactions", "aireload", "forget",
    "autoreply", "files", "modules", "reloadmod", "unloadmod", "ftgconfig", "dlmod", "dlnative",
    "modconfirm", "modcancel",
}


def configure(client, owner_id: int, prefix: str = ".", state_path: Optional[str] = None):
    global CLIENT, OWNER_ID, PREFIX, DB
    CLIENT = client
    OWNER_ID = int(owner_id)
    PREFIX = prefix or "."
    default_state = Path(FTG_MODULES_PATH).parent / "data" / "ftg_db.json"
    DB = PersistentDB(Path(state_path) if state_path else default_state)


def set_client(client):
    global CLIENT
    CLIENT = client


def _module_key(path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:10]
    return f"{path.stem}_{digest}"


def _import_module(path: Path, package: str):
    source = path.read_text(encoding="utf-8-sig")
    compile(source, str(path), "exec")
    critical, warnings = ModuleInstaller.scan(source)
    if critical:
        raise ValueError("Модуль заблокирован до импорта: " + "; ".join(critical))
    for warning in warnings:
        logger.warning(f"{path.name}: {warning}")
    key = _module_key(path)
    module_name = f"{package}.{key}"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if not spec or not spec.loader:
        raise ImportError(f"Не удалось импортировать {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _find_module_class(module):
    candidates = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if obj.__module__ != module.__name__:
            continue
        if issubclass(obj, Module) or isinstance(getattr(obj, "strings", None), dict):
            candidates.append(obj)
    if not candidates:
        raise ValueError("Не найден класс FTG-модуля с атрибутом strings")
    return candidates[0]


def _normalize_instance(instance, module_name: str):
    class_strings = getattr(type(instance), "strings", {"name": module_name})
    if not isinstance(getattr(instance, "strings", None), StringsProxy):
        localized = dict(class_strings) if isinstance(class_strings, dict) else {}
        strings_ru = getattr(type(instance), "strings_ru", {})
        if isinstance(strings_ru, dict):
            localized.update(strings_ru)
        instance.strings = StringsProxy(localized)
    instance.name = str(instance.strings.get("name", module_name))
    if not hasattr(instance, "config"):
        instance.config = ModuleConfig()
    if isinstance(instance.config, ModuleConfig) and DB is not None:
        sentinel = object()
        namespace = f"{instance.name}.config"
        for key in list(instance.config):
            saved = DB.get(namespace, key, sentinel)
            if saved is sentinel:
                continue
            try:
                instance.config.set(key, saved)
            except Exception:
                logger.warning(f"Некорректное сохранённое значение {instance.name}.{key} пропущено")
    instance._client = CLIENT
    instance.client = CLIENT
    instance._db = DB
    instance._tg_id = OWNER_ID
    instance.tg_id = OWNER_ID
    instance.allmodules = ALLMODULES
    instance.inline = InlineStub()
    instance.prefix = PREFIX
    instance._prefix = PREFIX
    return instance


async def _call_optional(instance, method_name: str, *args):
    method = getattr(instance, method_name, None)
    if not callable(method):
        return None
    signature = inspect.signature(method)
    parameters = list(signature.parameters.values())
    accepts_varargs = any(item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters)
    positional = [
        item
        for item in parameters
        if item.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    selected_args = args if accepts_varargs else args[:len(positional)]
    result = method(*selected_args)
    return await result if inspect.isawaitable(result) else result


def _command_methods(instance):
    found = {}
    declared = getattr(instance, "commands", {})
    if isinstance(declared, dict):
        for name, method in declared.items():
            if isinstance(method, str):
                method = getattr(instance, method, None)
            if callable(method):
                found[str(name).lower()] = method

    for attr_name in dir(instance):
        method = getattr(instance, attr_name, None)
        if not callable(method):
            continue
        marked = hasattr(method, "ftg_command")
        if not marked and not attr_name.endswith("cmd"):
            continue
        explicit = getattr(method, "ftg_command", None)
        name = str(explicit or attr_name[:-3]).lower()
        found[name] = method
        options = getattr(method, "ftg_command_options", {}) or {}
        for alias in options.get("aliases", []) or []:
            found[str(alias).lower()] = method
    return found


def _watcher_methods(instance):
    found = []
    for attr_name in dir(instance):
        method = getattr(instance, attr_name, None)
        if callable(method) and (getattr(method, "ftg_watcher", False) or attr_name.endswith("watcher")):
            found.append(method)
    return found


def _watcher_allowed(event, options: dict) -> bool:
    filters = {str(item).lower() for item in options.get("filters", [])}
    filters.update(str(key).lower() for key, enabled in options.items() if enabled is True)
    if (options.get("out") or "out" in filters) and not event.out:
        return False
    if (options.get("in") or "in" in filters) and event.out:
        return False
    if (options.get("private") or "private" in filters) and not event.is_private:
        return False
    if (options.get("groups") or "groups" in filters) and not event.is_group:
        return False
    if (options.get("channels") or "channels" in filters) and not event.is_channel:
        return False
    if ("only_pm" in filters or "pm" in filters) and not event.is_private:
        return False
    if "only_groups" in filters and not event.is_group:
        return False
    if "only_channels" in filters and not event.is_channel:
        return False
    text = str(getattr(event, "raw_text", "") or "")
    is_command = bool(PREFIX and text.startswith(PREFIX))
    if "no_commands" in filters and is_command:
        return False
    if "only_commands" in filters and not is_command:
        return False
    chats = options.get("chats")
    if chats and event.chat_id not in ({chats} if isinstance(chats, int) else set(chats)):
        return False
    blacklist = options.get("blacklist_chats")
    if blacklist and event.chat_id in ({blacklist} if isinstance(blacklist, int) else set(blacklist)):
        return False
    users = options.get("from_users")
    users = users or options.get("from_id")
    if users and event.sender_id not in ({users} if isinstance(users, int) else set(users)):
        return False
    pattern = options.get("regex") or options.get("pattern")
    if pattern and not re.search(pattern, text):
        return False
    return True


async def _run_command(method, event, command_name: str):
    try:
        result = method(event.message)
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(f"Ошибка FTG-команды {command_name}")
        text = f"Ошибка модуля {command_name}: {type(exc).__name__}: {exc}"
        try:
            await event.edit(text[:3900], parse_mode=None)
        except Exception:
            logger.error(traceback.format_exc())


async def _run_watcher(method, event, module_name: str):
    try:
        result = method(event.message)
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(f"Ошибка watcher модуля {module_name}")


async def _loop_runner(method, module_name: str):
    interval = float(getattr(method, "ftg_loop_interval", 5))
    if getattr(method, "ftg_loop_wait_before", False):
        await asyncio.sleep(interval)
    while True:
        try:
            result = method()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"Ошибка loop модуля {module_name}")
        await asyncio.sleep(interval)


async def load_ftg_module(module_name: str, client=None):
    client = client or CLIENT
    if client is None or DB is None:
        raise RuntimeError("Сначала вызови configure()")
    path = Path(FTG_MODULES_PATH) / f"{Path(module_name).stem}.py"
    if not path.exists():
        raise FileNotFoundError(f"FTG-модуль {path.name} не найден")
    key = _module_key(path)
    if key in LOADED_MODULES:
        await unload_module(key)

    module = _import_module(path, "userbot.ftg_modules")
    cls = _find_module_class(module)
    instance = _normalize_instance(cls(), path.stem)
    record = ModuleRecord(
        key=key,
        name=instance.name,
        kind="ftg",
        instance=instance,
        module=module,
        path=path,
    )
    LOADED_MODULES[key] = record
    ALLMODULES.modules.append(instance)

    try:
        await _call_optional(instance, "client_ready", client, DB)
        await _call_optional(instance, "on_dlmod", client, DB)

        commands = _command_methods(instance)
        collisions = sorted(set(commands) & (set(LOADED_HANDLERS) | RESERVED_COMMANDS))
        if collisions:
            raise ValueError("Команды уже заняты другими FTG-модулями: " + ", ".join(collisions))

        for command_name, method in commands.items():
            pattern = rf"^{re.escape(PREFIX)}{re.escape(command_name)}(?:\s|$)"

            async def callback(event, bound=method, name=command_name):
                if not event.out or (OWNER_ID and event.sender_id not in {None, OWNER_ID}):
                    return
                await _run_command(bound, event, name)

            builder = events.NewMessage(pattern=pattern, outgoing=True)
            client.add_event_handler(callback, builder)
            record.handlers.append(HandlerRef(callback, builder))
            record.commands.append(command_name)
            LOADED_HANDLERS[command_name] = callback
            ALLMODULES.commands[command_name] = method

        for method in _watcher_methods(instance):
            options = dict(getattr(method, "ftg_tags", {}) or {})
            options.update(getattr(method, "ftg_watcher_options", {}) or {})

            async def watcher_callback(event, bound=method, opts=options, name=instance.name):
                if _watcher_allowed(event, opts):
                    await _run_watcher(bound, event, name)

            builder = events.NewMessage()
            client.add_event_handler(watcher_callback, builder)
            record.handlers.append(HandlerRef(watcher_callback, builder))
            ALLMODULES.watchers.append(method)

        if hasattr(events, "Raw"):
            for attr_name in dir(instance):
                method = getattr(instance, attr_name, None)
                raw_types = getattr(method, "ftg_raw_handler", None) if callable(method) else None
                if raw_types is None:
                    continue

                async def raw_callback(update, bound=method, name=instance.name):
                    try:
                        result = bound(update)
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        logger.exception(f"Ошибка raw handler модуля {name}")

                builder = events.Raw(types=raw_types) if raw_types else events.Raw()
                client.add_event_handler(raw_callback, builder)
                record.handlers.append(HandlerRef(raw_callback, builder))

        for attr_name in dir(instance):
            method = getattr(instance, attr_name, None)
            if not callable(method) or not getattr(method, "ftg_callback_handler", False):
                continue

            async def callback_query(event, bound=method, name=instance.name):
                try:
                    result = bound(InlineCall(event))
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.exception(f"Ошибка callback handler модуля {name}")

            builder = events.CallbackQuery()
            client.add_event_handler(callback_query, builder)
            record.handlers.append(HandlerRef(callback_query, builder))

        for attr_name in dir(instance):
            method = getattr(instance, attr_name, None)
            if callable(method) and getattr(method, "ftg_loop", False):
                if hasattr(method, "start"):
                    task = method.task if getattr(method, "running", False) else None
                    if task is None and getattr(method, "ftg_loop_autostart", False):
                        task = method.start()
                else:
                    task = None
                    if getattr(method, "ftg_loop_autostart", False):
                        task = asyncio.create_task(_loop_runner(method, instance.name))
                if task and task not in record.tasks:
                    record.tasks.append(task)

        logger.info(f"FTG-модуль {instance.name} загружен; команды: {record.commands}")
        return instance
    except (SelfUnload, SelfSuspend) as exc:
        await unload_module(key)
        logger.info(f"FTG-модуль {record.name} отказался от загрузки: {exc}")
        return None
    except Exception:
        await unload_module(key)
        raise


async def load_native_module(path: str, client=None):
    client = client or CLIENT
    file_path = Path(path)
    module = _import_module(file_path, "userbot.native_modules")
    register_method = getattr(module, "register", None)
    if not callable(register_method):
        raise ValueError("Native-модуль не содержит register(client)")
    key = _module_key(file_path)
    if key in LOADED_MODULES:
        await unload_module(key)
    before = list(client.list_event_handlers()) if hasattr(client, "list_event_handlers") else []
    try:
        result = register_method(client)
        instance = await result if inspect.isawaitable(result) else result
    except Exception:
        if hasattr(client, "list_event_handlers"):
            for callback, builder in client.list_event_handlers():
                if (callback, builder) not in before:
                    client.remove_event_handler(callback, builder)
        raise
    record = ModuleRecord(key, file_path.stem, "native", instance or module, module, file_path)
    if hasattr(client, "list_event_handlers"):
        for callback, builder in client.list_event_handlers():
            if (callback, builder) not in before:
                record.handlers.append(HandlerRef(callback, builder))
    LOADED_MODULES[key] = record
    logger.info(f"Native-модуль {file_path.name} загружен")
    return instance or module


async def unload_module(name: str) -> bool:
    normalized = str(name).lower()
    key = next(
        (
            item_key
            for item_key, record in LOADED_MODULES.items()
            if normalized in {item_key.lower(), record.name.lower(), record.path.stem.lower()}
        ),
        None,
    )
    if not key:
        return False
    record = LOADED_MODULES.pop(key)
    for ref in record.handlers:
        try:
            CLIENT.remove_event_handler(ref.callback, ref.builder)
        except Exception:
            logger.exception(f"Не удалось удалить handler {record.name}")
    for task in record.tasks:
        task.cancel()
    if record.tasks:
        await asyncio.gather(*record.tasks, return_exceptions=True)
    try:
        await _call_optional(record.instance, "on_unload")
    except Exception:
        logger.exception(f"Ошибка on_unload модуля {record.name}")
    for command_name in record.commands:
        LOADED_HANDLERS.pop(command_name, None)
        ALLMODULES.commands.pop(command_name, None)
    ALLMODULES.modules = [item for item in ALLMODULES.modules if item is not record.instance]
    ALLMODULES.watchers = [item for item in ALLMODULES.watchers if item not in _watcher_methods(record.instance)]
    sys.modules.pop(record.module.__name__, None)
    logger.info(f"Модуль {record.name} выгружен")
    return True


async def reload_module(name: str):
    normalized = str(name).lower()
    record = next(
        (
            item
            for item in LOADED_MODULES.values()
            if normalized in {item.key.lower(), item.name.lower(), item.path.stem.lower()}
        ),
        None,
    )
    if not record:
        raise ValueError("Модуль не найден")
    path = record.path
    kind = record.kind
    await unload_module(record.key)
    if kind == "ftg":
        return await load_ftg_module(path.stem)
    return await load_native_module(str(path))


async def load_all_ftg_modules(client=None):
    Path(FTG_MODULES_PATH).mkdir(parents=True, exist_ok=True)
    results = []
    for path in sorted(Path(FTG_MODULES_PATH).glob("*.py")):
        if path.name.startswith("_") or path.name.endswith(".disabled.py"):
            continue
        try:
            results.append(await load_ftg_module(path.stem, client))
        except Exception:
            logger.exception(f"FTG-модуль {path.name} пропущен")
    return results


async def load_all_native_modules(client=None):
    Path(NATIVE_MODULES_PATH).mkdir(parents=True, exist_ok=True)
    results = []
    for path in sorted(Path(NATIVE_MODULES_PATH).glob("*.py")):
        if path.name.startswith("_") or path.name.endswith(".disabled.py"):
            continue
        try:
            results.append(await load_native_module(str(path), client))
        except Exception:
            logger.exception(f"Native-модуль {path.name} пропущен")
    return results


async def load_all_modules(client=None):
    native = await load_all_native_modules(client)
    ftg = await load_all_ftg_modules(client)
    return native + ftg


def _find_record(name: str) -> Optional[ModuleRecord]:
    normalized = str(name).strip().lower()
    return next(
        (
            record
            for record in LOADED_MODULES.values()
            if normalized in {record.key.lower(), record.name.lower(), record.path.stem.lower()}
        ),
        None,
    )


def module_config_text(name: str) -> str:
    record = _find_record(name)
    if not record:
        raise ValueError("Модуль не найден")
    config = getattr(record.instance, "config", None)
    if not isinstance(config, ModuleConfig) or not config:
        return f"У модуля {record.name} нет параметров ModuleConfig."
    lines = [f"Конфиг {record.name}:"]
    for key, value in config.items():
        definition = config._values.get(key)
        doc = config.getdoc(key) if definition else None
        suffix = f" — {doc}" if isinstance(doc, str) and doc else ""
        lines.append(f"{key} = {value!r}{suffix}")
    return "\n".join(lines)


def set_module_config(name: str, key: str, value: Any) -> Any:
    record = _find_record(name)
    if not record:
        raise ValueError("Модуль не найден")
    config = getattr(record.instance, "config", None)
    if not isinstance(config, ModuleConfig) or key not in config:
        raise ValueError(f"Параметр {key!r} не найден")
    definition = config._values.get(key)
    if not definition or not definition.validator:
        current = config[key]
        if isinstance(current, bool):
            normalized = str(value).strip().lower()
            if normalized not in {"1", "0", "true", "false", "yes", "no", "on", "off", "да", "нет"}:
                raise ValueError("Ожидалось логическое значение")
            value = normalized in {"1", "true", "yes", "on", "да"}
        elif isinstance(current, int) and not isinstance(current, bool):
            value = int(value)
        elif isinstance(current, float):
            value = float(value)
    config.set(key, value)
    if DB is not None:
        DB.set(f"{record.name}.config", key, config[key])
    return config[key]


def module_status_text() -> str:
    if not LOADED_MODULES:
        return "Модули не загружены."
    lines = []
    for record in sorted(LOADED_MODULES.values(), key=lambda item: item.name.lower()):
        commands = ", ".join(f"{PREFIX}{x}" for x in sorted(set(record.commands))) or "нет команд"
        lines.append(f"{record.name} [{record.kind}] — {commands}")
    return "\n".join(lines)


def loaded_module_count() -> int:
    return len(LOADED_MODULES)


def _help_block(title: str, description: str, commands: list[tuple[str, str]]) -> str:
    command_lines = "\n".join(
        f"<code>{html.escape(command)}</code> — {html.escape(explanation)}"
        for command, explanation in commands
    )
    return (
        f"<b>{html.escape(title)}</b>\n"
        f"<blockquote>{html.escape(description)}\n\n{command_lines}</blockquote>"
    )


def _help_sections() -> dict[str, str]:
    sections = {
        "system": _help_block(
            "Системные команды",
            "Состояние юзербота и навигация по справке.",
            [
                (f"{PREFIX}help [system|ai|files|modules|ftg|panel]", "открыть всю справку или раздел"),
                (f"{PREFIX}syshelp", "только системный раздел"),
                (f"{PREFIX}ping", "проверить задержку обработчика"),
                (f"{PREFIX}uptime", "показать время работы"),
                (f"{PREFIX}id", "показать ID аккаунта, чата и сообщения в реплае"),
            ],
        ),
        "ai": _help_block(
            "AI и Ollama",
            "Ответы Faust, Telegram-действия и контроль доступа.",
            [
                (f"{PREFIX}фауст <текст>", "задать вопрос или попросить выполнить действие"),
                (f"{PREFIX}aihelp", "только AI-раздел"),
                (f"{PREFIX}health", "проверить Ollama и модель"),
                (f"{PREFIX}aistats", "показать очередь и скорость AI"),
                (f"{PREFIX}aiactions", "показать все AI-действия"),
                (f"{PREFIX}aireload", "перечитать prompt и AI-блоки"),
                (f"{PREFIX}forget", "очистить историю текущего чата"),
                (f"{PREFIX}answer [on|off] [ID|@username]", "управлять обычными автоответами"),
                (f"{PREFIX}autoreply", "показать новые правила автоответчика"),
                (f"{PREFIX}autoreply persona on all", "отвечать от своего имени всем"),
                (f"{PREFIX}autoreply assistant on chat -100...", "AI-ассистент для группы по ID"),
                (f"{PREFIX}autoreply persona on user 123", "ответы от своего имени пользователю"),
                (f"{PREFIX}autoreply persona on contacts", "ответы контактам; есть noncontacts"),
                (f"{PREFIX}bypass [on|off] [ID|@username]", "управлять доступом к AI-действиям"),
                (f"{PREFIX}confirm <код>", "подтвердить опасное AI-действие"),
                (f"{PREFIX}cancel", "отменить ожидающие AI-действия"),
            ],
        ),
        "files": _help_block(
            "Файлы и ярлыки",
            "Файлы сохраняются внутри userbot/data/files и затем находятся по понятному ярлыку.",
            [
                (f"{PREFIX}filehelp", "только раздел файлов"),
                (f"{PREFIX}files", "показать сохранённые файлы"),
                (f"{PREFIX}фауст сохрани этот файл с ярлыком договор", "скачать медиа из реплая"),
                (f"{PREFIX}фауст скинь файл договор Артему", "найти диалог по имени и отправить файл"),
                (f"{PREFIX}фауст скинь файл договор пользователю 123456", "отправить по Telegram ID"),
            ],
        ),
        "modules": _help_block(
            "Управление модулями",
            "Загрузка проходит через карантин; код начинает работать только после подтверждения.",
            [
                (f"{PREFIX}modhelp", "только раздел управления модулями"),
                (f"{PREFIX}modules", "показать загруженные модули"),
                (f"{PREFIX}reloadmod <имя>", "перезагрузить модуль"),
                (f"{PREFIX}unloadmod <имя>", "выгрузить модуль"),
                (f"{PREFIX}dlmod [HTTPS-ссылка]", "подготовить FTG/Hikka-модуль"),
                (f"{PREFIX}dlnative [HTTPS-ссылка]", "подготовить нативный модуль"),
                (f"{PREFIX}modconfirm <код>", "установить подготовленный модуль"),
                (f"{PREFIX}modcancel", "отменить все ожидающие установки"),
            ],
        ),
        "ftg": _help_block(
            "FTG/Hikka-команды",
            f"Совместимость с FTG/Hikka. Сейчас загружено модулей: {len(LOADED_MODULES)}.",
            [
                (f"{PREFIX}ftghelp", "только FTG-раздел"),
                (f"{PREFIX}ftgconfig <модуль>", "посмотреть ModuleConfig"),
                (f"{PREFIX}ftgconfig <модуль> <ключ> <значение>", "изменить параметр модуля"),
                (f"{PREFIX}modules", "посмотреть команды каждого FTG-модуля"),
            ],
        ),
        "panel": _help_block(
            "Панель управления",
            "Без аргумента открывается inline-панель. В разделе автоответа есть кнопки для всех и текущего чата. Если inline недоступен, используй команды ниже.",
            [
                (f"{PREFIX}panel", "открыть inline-панель"),
                (f"{PREFIX}panelhelp", "только раздел панели"),
                (f"{PREFIX}panel status", "состояние юзербота"),
                (f"{PREFIX}panel health", "проверка Ollama"),
                (f"{PREFIX}panel ai", "статистика AI"),
                (f"{PREFIX}panel modules", "список модулей"),
                (f"{PREFIX}panel actions", "список AI-действий"),
                (f"{PREFIX}panel autoreply", "правила автоответчика"),
                (f"{PREFIX}panel files", "список сохранённых файлов"),
                (f"{PREFIX}panel reload", "перезагрузить AI-блоки"),
                (f"{PREFIX}panel retry", "повторить открытие inline-панели"),
            ],
        ),
    }
    return sections


def get_help_text(section: Optional[str] = None) -> str:
    aliases = {
        "sys": "system", "system": "system", "система": "system",
        "ai": "ai", "ии": "ai",
        "file": "files", "files": "files", "файлы": "files",
        "mod": "modules", "module": "modules", "modules": "modules", "модули": "modules",
        "ftg": "ftg", "hikka": "ftg",
        "panel": "panel", "панель": "panel",
    }
    sections = _help_sections()
    requested = aliases.get(str(section or "").strip().lower())
    if section and not requested:
        return (
            "<b>Раздел справки не найден</b>\n"
            f"<blockquote>Используй <code>{html.escape(PREFIX)}help system</code>, "
            "<code>ai</code>, <code>files</code>, <code>modules</code>, <code>ftg</code> или <code>panel</code>.</blockquote>"
        )
    if requested:
        return sections[requested]
    intro = (
        "<b>Faust Tool v3 — справка</b>\n"
        "<blockquote>Разделы можно открывать отдельно командами "
        f"<code>{html.escape(PREFIX)}syshelp</code>, <code>{html.escape(PREFIX)}aihelp</code>, "
        f"<code>{html.escape(PREFIX)}filehelp</code>, <code>{html.escape(PREFIX)}modhelp</code>, <code>{html.escape(PREFIX)}ftghelp</code> и "
        f"<code>{html.escape(PREFIX)}panelhelp</code>.</blockquote>"
    )
    separator = "\n\n━━━━━━━━━━━━━━━━━━\n\n"
    return intro + separator + separator.join(sections.values())


def get_help_entities():
    return get_help_text(), []
