from __future__ import annotations

import asyncio
import functools
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional


class StringsProxy(dict):
    def __call__(self, key: str, *args, **kwargs) -> str:
        return str(self.get(key, key))


class ValidationError(ValueError):
    pass


class LoadError(RuntimeError):
    """Понятная ошибка загрузки, совместимая с Hikka/FTG-модулями."""


class SelfUnload(RuntimeError):
    """Модуль может выбросить исключение, чтобы отказаться от загрузки."""


class SelfSuspend(RuntimeError):
    """Совместимый маркер временной приостановки модуля."""


class _Validator:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __call__(self, value):
        return self.validate(value)

    def validate(self, value):
        return value


class Boolean(_Validator):
    def validate(self, value):
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on", "да"}:
            return True
        if normalized in {"0", "false", "no", "off", "нет"}:
            return False
        raise ValidationError("Ожидалось логическое значение")


class Integer(_Validator):
    def validate(self, value):
        value = int(value)
        minimum = self.kwargs.get("minimum")
        maximum = self.kwargs.get("maximum")
        if minimum is not None and value < minimum:
            raise ValidationError(f"Минимум: {minimum}")
        if maximum is not None and value > maximum:
            raise ValidationError(f"Максимум: {maximum}")
        return value


class Float(_Validator):
    def validate(self, value):
        value = float(value)
        minimum = self.kwargs.get("minimum")
        maximum = self.kwargs.get("maximum")
        if minimum is not None and value < minimum:
            raise ValidationError(f"Минимум: {minimum}")
        if maximum is not None and value > maximum:
            raise ValidationError(f"Максимум: {maximum}")
        return value


class String(_Validator):
    def validate(self, value):
        value = str(value)
        minimum = self.kwargs.get("min_len", self.kwargs.get("minimum"))
        maximum = self.kwargs.get("max_len", self.kwargs.get("maximum"))
        fixed = self.kwargs.get("fixed_len")
        if fixed is not None and len(value) != int(fixed):
            raise ValidationError(f"Длина должна быть ровно {fixed}")
        if minimum is not None and len(value) < int(minimum):
            raise ValidationError(f"Минимальная длина: {minimum}")
        if maximum is not None and len(value) > int(maximum):
            raise ValidationError(f"Максимальная длина: {maximum}")
        return value


class Choice(_Validator):
    def __init__(self, possible_values: Iterable[Any], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.values = list(possible_values)

    def validate(self, value):
        if value not in self.values:
            raise ValidationError(f"Допустимые значения: {self.values}")
        return value


class MultiChoice(Choice):
    def validate(self, value):
        values = list(value) if isinstance(value, (list, tuple, set)) else [value]
        invalid = [item for item in values if item not in self.values]
        if invalid:
            raise ValidationError(f"Недопустимые значения: {invalid}")
        return values


class Series(_Validator):
    def __init__(self, validator: Optional[Callable] = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.validator = validator

    def validate(self, value):
        values = list(value) if isinstance(value, (list, tuple, set)) else [value]
        minimum = self.kwargs.get("min_len")
        maximum = self.kwargs.get("max_len")
        fixed = self.kwargs.get("fixed_len")
        if fixed is not None and len(values) != int(fixed):
            raise ValidationError(f"Количество элементов должно быть ровно {fixed}")
        if minimum is not None and len(values) < int(minimum):
            raise ValidationError(f"Минимум элементов: {minimum}")
        if maximum is not None and len(values) > int(maximum):
            raise ValidationError(f"Максимум элементов: {maximum}")
        return [self.validator(item) if self.validator else item for item in values]


class Union(_Validator):
    def __init__(self, *validators, **kwargs):
        super().__init__(**kwargs)
        self.validators = validators

    def validate(self, value):
        for validator in self.validators:
            try:
                return validator(value)
            except Exception:
                continue
        raise ValidationError("Значение не прошло ни одну проверку")


class NoneType(_Validator):
    def validate(self, value):
        if value is not None:
            raise ValidationError("Ожидалось None")
        return None


class RegExp(_Validator):
    def __init__(self, regex: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.regex = re.compile(regex)

    def validate(self, value):
        value = str(value)
        if not self.regex.fullmatch(value):
            raise ValidationError("Значение не соответствует шаблону")
        return value


class Link(RegExp):
    def __init__(self, *args, **kwargs):
        super().__init__(r"https?://.+", *args, **kwargs)


class TelegramID(Integer):
    pass


class EntityLike(_Validator):
    def validate(self, value):
        if isinstance(value, int):
            return value
        value = str(value).strip()
        if not value:
            raise ValidationError("Пустая ссылка на сущность")
        return value


class Hidden(_Validator):
    def __init__(self, validator: Optional[Callable] = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.validator = validator

    def validate(self, value):
        return self.validator(value) if self.validator else value


class Emoji(String):
    pass


class validators:
    ValidationError = ValidationError
    Boolean = Boolean
    Integer = Integer
    Float = Float
    String = String
    Choice = Choice
    MultiChoice = MultiChoice
    Series = Series
    Union = Union
    NoneType = NoneType
    RegExp = RegExp
    Link = Link
    TelegramID = TelegramID
    EntityLike = EntityLike
    Hidden = Hidden
    Emoji = Emoji


class ConfigValue:
    def __init__(
        self,
        option: str,
        default: Any = None,
        doc: Any = None,
        validator: Optional[Callable] = None,
        on_change: Optional[Callable] = None,
        **kwargs,
    ):
        self.option = str(option)
        self.default = default
        self.doc = doc
        self.validator = validator
        self.on_change = on_change
        self.extra = kwargs


class ModuleConfig(dict):
    def __init__(self, *values: ConfigValue, **kwargs):
        super().__init__(kwargs)
        self._values = {}
        normalized = []
        index = 0
        while index < len(values):
            item = values[index]
            if isinstance(item, ConfigValue):
                normalized.append(item)
                index += 1
                continue
            if isinstance(item, str) and index + 1 < len(values):
                default = values[index + 1]
                doc = values[index + 2] if index + 2 < len(values) else None
                normalized.append(ConfigValue(item, default, doc))
                index += 3
                continue
            index += 1
        for item in normalized:
            if not isinstance(item, ConfigValue):
                continue
            self._values[item.option] = item
            self[item.option] = item.default

    def set(self, key: str, value: Any) -> None:
        config_value = self._values.get(key)
        if config_value and config_value.validator:
            value = config_value.validator(value)
        self[key] = value
        if config_value and config_value.on_change:
            result = config_value.on_change()
            if asyncio.iscoroutine(result):
                try:
                    asyncio.get_running_loop().create_task(result)
                except RuntimeError:
                    result.close()

    def getdoc(self, key: str) -> Any:
        value = self._values.get(key)
        if value is None:
            return None
        return value.doc() if callable(value.doc) else value.doc

    def getdef(self, key: str) -> Any:
        value = self._values.get(key)
        return value.default if value is not None else None


def _decorator(marker: str, value: Any = True, **metadata):
    def apply(func):
        setattr(func, marker, value)
        for key, item in metadata.items():
            setattr(func, key, item)
        return func

    return apply


def command(*args, **kwargs):
    if args and callable(args[0]) and len(args) == 1:
        return _decorator("ftg_command", kwargs.get("name"), ftg_command_options=kwargs)(args[0])
    name = args[0] if args and isinstance(args[0], str) else kwargs.get("name")
    return _decorator("ftg_command", name, ftg_command_options=kwargs)


def watcher(*args, **kwargs):
    if args and callable(args[0]) and len(args) == 1:
        return _decorator("ftg_watcher", True, ftg_watcher_options=kwargs)(args[0])
    options = dict(kwargs)
    if args:
        options["filters"] = list(args)
    return _decorator("ftg_watcher", True, ftg_watcher_options=options)


def tag(*tags, **options):
    """Поддерживает @loader.tag("no_commands", out=True) из Hikka."""

    if tags and callable(tags[0]) and len(tags) == 1:
        return tag(**options)(tags[0])

    normalized = {str(item).strip().lower(): True for item in tags if str(item).strip()}
    normalized.update({str(key).lower(): value for key, value in options.items()})

    def decorator(func):
        current = dict(getattr(func, "ftg_tags", {}) or {})
        current.update(normalized)
        setattr(func, "ftg_tags", current)
        return func

    return decorator


def owner(func=None):
    decorator = _decorator("ftg_owner", True)
    return decorator(func) if callable(func) else decorator


def sudo(func=None):
    decorator = _decorator("ftg_owner", True)
    return decorator(func) if callable(func) else decorator


def _security_decorator(name: str):
    def factory(func=None):
        decorator = _decorator("ftg_security", name)
        return decorator(func) if callable(func) else decorator

    return factory


support = _security_decorator("support")
group_owner = _security_decorator("group_owner")
group_admin = _security_decorator("group_admin")
group_admin_add_admins = _security_decorator("group_admin_add_admins")
group_admin_change_info = _security_decorator("group_admin_change_info")
group_admin_ban_users = _security_decorator("group_admin_ban_users")
group_admin_delete_messages = _security_decorator("group_admin_delete_messages")
group_admin_pin_messages = _security_decorator("group_admin_pin_messages")
group_member = _security_decorator("group_member")
pm = _security_decorator("pm")


def everyone(func=None):
    return unrestricted(func)


def unrestricted(func=None):
    decorator = _decorator("ftg_unrestricted", True)
    return decorator(func) if callable(func) else decorator


def tds(cls):
    return cls


def register(func):
    return func


def callback_handler(*args, **kwargs):
    if args and callable(args[0]):
        return _decorator("ftg_callback_handler", True)(args[0])
    return _decorator("ftg_callback_handler", True)


def inline_handler(*args, **kwargs):
    if args and callable(args[0]):
        return _decorator("ftg_inline_handler", True)(args[0])
    return _decorator("ftg_inline_handler", True)


def inline_everyone(func=None):
    decorator = _decorator("ftg_inline_everyone", True)
    return decorator(func) if callable(func) else decorator


def raw_handler(*types):
    if types and callable(types[0]) and len(types) == 1:
        return _decorator("ftg_raw_handler", ())(types[0])
    return _decorator("ftg_raw_handler", tuple(types))


def message_handler(*args, **kwargs):
    return watcher(*args, **kwargs)


class _BoundLoop:
    def __init__(self, descriptor, instance):
        self.descriptor = descriptor
        self.instance = instance
        self.ftg_loop = True
        self.ftg_loop_interval = descriptor.interval
        self.ftg_loop_autostart = descriptor.autostart
        self.ftg_loop_wait_before = descriptor.wait_before

    def __call__(self, *args, **kwargs):
        return self.descriptor.func(self.instance, *args, **kwargs)

    @property
    def task(self):
        return self.descriptor.tasks.get(id(self.instance))

    @property
    def running(self):
        task = self.task
        return bool(task and not task.done())

    async def _runner(self):
        if self.descriptor.wait_before:
            await asyncio.sleep(self.descriptor.interval)
        while True:
            try:
                await self()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.descriptor.interval)

    def start(self):
        if self.running:
            return self.task
        task = asyncio.create_task(self._runner())
        self.descriptor.tasks[id(self.instance)] = task
        return task

    def stop(self):
        task = self.task
        if task:
            task.cancel()
        return task

    def set_interval(self, interval: float):
        self.descriptor.interval = max(0.1, float(interval))
        self.ftg_loop_interval = self.descriptor.interval
        return self.descriptor.interval


class _LoopDescriptor:
    def __init__(self, func, interval, autostart, wait_before):
        functools.update_wrapper(self, func)
        self.func = func
        self.interval = interval
        self.autostart = autostart
        self.wait_before = wait_before
        self.tasks = {}
        self.ftg_loop = True

    def __get__(self, instance, owner):
        return self if instance is None else _BoundLoop(self, instance)


def loop(interval: float = 5, autostart: bool = False, wait_before: bool = False, **kwargs):
    def decorator(func):
        return _LoopDescriptor(
            func,
            max(0.1, float(interval)),
            bool(autostart),
            bool(wait_before),
        )

    return decorator


def ratelimit(limit: float = 1):
    def decorator(func):
        last_calls = {}

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            message = args[-1] if args else None
            key = getattr(message, "sender_id", None) or "global"
            now = time.monotonic()
            if now - last_calls.get(key, 0) < float(limit):
                return None
            last_calls[key] = now
            result = func(*args, **kwargs)
            if asyncio.iscoroutine(result):
                return await result
            return result

        for name in (
            "ftg_command", "ftg_command_options", "ftg_watcher", "ftg_watcher_options",
            "ftg_owner", "ftg_unrestricted", "ftg_tags",
        ):
            if hasattr(func, name):
                setattr(wrapper, name, getattr(func, name))
        return wrapper

    return decorator


class Module:
    strings = {"name": "Module"}
    strings_ru = {}
    commands = {}

    def __init__(self):
        source = getattr(type(self), "strings", {"name": type(self).__name__})
        localized = dict(source) if isinstance(source, dict) else {}
        russian = getattr(type(self), "strings_ru", {})
        if isinstance(russian, dict):
            localized.update(russian)
        self.strings = StringsProxy(localized)
        self.name = self.strings.get("name", type(self).__name__)
        self.config = getattr(self, "config", ModuleConfig())
        self._client = None
        self.client = None
        self._db = None
        self.allmodules = None
        self.inline = None
        self.prefix = "."

    async def client_ready(self, client, db):
        self._client = client
        self.client = client
        self._db = db

    def get(self, key, default=None):
        return self._db.get(self.name, key, default) if self._db else default

    def set(self, key, value):
        if self._db:
            self._db.set(self.name, key, value)
        return value

    def pointer(self, key, default=None):
        return self._db.pointer(self.name, key, default) if self._db else default

    def get_prefix(self):
        return self.prefix

    def lookup(self, name):
        return self.allmodules.lookup(name) if self.allmodules else None

    async def invoke(self, command_name, args="", peer=None):
        if not self.allmodules:
            raise RuntimeError("Реестр модулей не настроен")
        return await self.allmodules.invoke(command_name, args=args, peer=peer)

    async def request_join(self, peer, reason=None):
        if not self._client:
            raise RuntimeError("Telegram-клиент не настроен")
        from telethon.tl.functions.channels import JoinChannelRequest

        return await self._client(JoinChannelRequest(peer))

    async def animate(self, message, frames, interval=0.5, inline=False):
        for frame in frames:
            await message.edit(frame)
            await asyncio.sleep(interval)
        return message
