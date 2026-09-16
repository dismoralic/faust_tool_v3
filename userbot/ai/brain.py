from __future__ import annotations

import asyncio
import inspect
import json
import re
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import aiohttp
from loguru import logger


class OllamaError(RuntimeError):
    pass


class OllamaBusyError(OllamaError):
    pass


@dataclass(frozen=True)
class ActionMeta:
    name: str
    description: str
    triggers: tuple[str, ...]
    params: tuple[dict, ...]
    risk: str = "read"
    confirmation: bool = False
    owner_only: bool = False


@dataclass
class ActionCandidate:
    name: str
    params: dict = field(default_factory=dict)
    confidence: float = 1.0
    source: str = "trigger"


@dataclass
class ActionPlanCandidate:
    steps: list[ActionCandidate]
    source: str = "neural_plan"


@dataclass
class PendingAction:
    token: str
    user_id: int
    action_name: str
    params: dict
    created_at: float
    expires_at: float
    client: Any
    message: Any


@dataclass
class PendingPlan:
    token: str
    user_id: int
    steps: list[tuple[str, dict]]
    created_at: float
    expires_at: float
    client: Any
    message: Any


def ai_function(
    *,
    description: str,
    triggers: Iterable[str],
    params: Optional[Iterable[dict]] = None,
    risk: str = "read",
    confirmation: bool = False,
    owner_only: bool = False,
    name: Optional[str] = None,
) -> Callable:
    if risk not in {"read", "write", "destructive"}:
        raise ValueError("risk должен быть read, write или destructive")

    def decorator(func: Callable) -> Callable:
        meta = ActionMeta(
            name=name or func.__name__,
            description=description,
            triggers=tuple(str(x).strip().lower() for x in triggers if str(x).strip()),
            params=tuple(dict(x) for x in (params or [])),
            risk=risk,
            confirmation=confirmation or risk == "destructive",
            owner_only=owner_only,
        )
        func._ai_function = True
        func._ai_meta = meta
        func._ai_triggers = list(meta.triggers)
        return func

    return decorator


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: int = 120,
        parallel: int = 1,
        max_queue: int = 20,
        keep_alive: str = "30m",
        num_ctx: int = 2048,
        num_predict: int = 256,
        retry_attempts: int = 1,
        circuit_threshold: int = 3,
        circuit_seconds: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = max(20, int(timeout))
        self.max_queue = max(1, int(max_queue))
        self.keep_alive = keep_alive
        self.num_ctx = max(512, int(num_ctx))
        self.num_predict = max(32, int(num_predict))
        self.retry_attempts = max(1, int(retry_attempts))
        self.circuit_threshold = max(1, int(circuit_threshold))
        self.circuit_seconds = max(5, int(circuit_seconds))
        self._semaphore = asyncio.Semaphore(max(1, int(parallel)))
        self._queue_lock = asyncio.Lock()
        self._waiting = 0
        self._active = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self.last_stats: dict = {}
        self.consecutive_failures = 0
        self.circuit_open_until = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout, connect=7, sock_read=self.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _strip_thinking(text: str) -> str:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.I | re.S)
        return text.strip()

    @staticmethod
    def _disable_thinking(messages: List[dict]) -> List[dict]:
        prepared = [dict(message) for message in messages]
        for index in range(len(prepared) - 1, -1, -1):
            if prepared[index].get("role") != "user":
                continue
            content = str(prepared[index].get("content") or "").rstrip()
            prepared[index]["content"] = f"{content}\n\n/no_think" if content else "/no_think"
            return prepared
        prepared.insert(0, {"role": "system", "content": "/no_think"})
        return prepared

    def _check_circuit(self) -> None:
        remaining = self.circuit_open_until - time.monotonic()
        if remaining > 0:
            raise OllamaError(f"Ollama временно отключена после ошибок, повтор через {remaining:.0f} сек.")

    def _mark_success(self) -> None:
        self.consecutive_failures = 0
        self.circuit_open_until = 0.0

    def _mark_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.circuit_threshold:
            self.circuit_open_until = time.monotonic() + self.circuit_seconds

    def _update_stats(self, data: dict, elapsed: float) -> None:
        eval_count = int(data.get("eval_count") or 0)
        eval_duration = int(data.get("eval_duration") or 0)
        prompt_count = int(data.get("prompt_eval_count") or 0)
        speed = eval_count / (eval_duration / 1_000_000_000) if eval_duration else 0.0
        self.last_stats = {
            "elapsed": round(elapsed, 3),
            "eval_count": eval_count,
            "prompt_eval_count": prompt_count,
            "tokens_per_second": round(speed, 2),
            "queue": self._waiting,
            "active": self._active,
            "model": data.get("model", self.model),
            "circuit_failures": self.consecutive_failures,
        }

    async def _request_chat(
        self,
        messages: List[dict],
        *,
        temperature: float,
        num_predict: int,
        num_ctx: Optional[int] = None,
        tools: Optional[list[dict]] = None,
        output_format: Any = None,
        ignore_circuit: bool = False,
    ) -> dict:
        if not ignore_circuit:
            self._check_circuit()
        queued = True
        async with self._queue_lock:
            if self._waiting >= self.max_queue:
                raise OllamaBusyError("Очередь Ollama заполнена, повторите немного позже")
            self._waiting += 1

        try:
            async with self._semaphore:
                async with self._queue_lock:
                    self._waiting -= 1
                    self._active += 1
                    queued = False
                try:
                    payload: dict = {
                        "model": self.model,
                        "messages": self._disable_thinking(messages),
                        "stream": False,
                        "think": False,
                        "keep_alive": self.keep_alive,
                        "options": {
                            "temperature": float(temperature),
                            "num_ctx": int(num_ctx or self.num_ctx),
                            "num_predict": int(num_predict),
                        },
                    }
                    if tools:
                        payload["tools"] = tools
                    if output_format is not None:
                        payload["format"] = output_format

                    session = await self._get_session()
                    started = time.monotonic()
                    last_error: Optional[Exception] = None
                    for attempt in range(1, self.retry_attempts + 1):
                        try:
                            async with session.post(f"{self.base_url}/api/chat", json=payload) as response:
                                body = await response.text()
                                if response.status >= 500 and attempt < self.retry_attempts:
                                    await asyncio.sleep(attempt * 1.5)
                                    continue
                                if response.status != 200:
                                    raise OllamaError(f"Ollama HTTP {response.status}: {body[:300]}")
                                try:
                                    data = json.loads(body)
                                except json.JSONDecodeError as exc:
                                    raise OllamaError("Ollama вернула некорректный JSON") from exc
                            elapsed = time.monotonic() - started
                            self._mark_success()
                            self._update_stats(data, elapsed)
                            return data
                        except asyncio.TimeoutError:
                            last_error = OllamaError(f"Ollama не ответила за {self.timeout} секунд")
                        except aiohttp.ClientError as exc:
                            last_error = OllamaError(f"Ошибка соединения с Ollama: {exc}")
                        except OllamaError as exc:
                            last_error = exc
                            if "HTTP 5" not in str(exc):
                                break
                        if attempt < self.retry_attempts:
                            await asyncio.sleep(attempt * 1.5)
                    self._mark_failure()
                    raise last_error or OllamaError("Неизвестная ошибка Ollama")
                finally:
                    async with self._queue_lock:
                        self._active = max(0, self._active - 1)
        finally:
            if queued:
                async with self._queue_lock:
                    self._waiting = max(0, self._waiting - 1)

    async def chat(
        self,
        messages: List[dict],
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        num_predict: Optional[int] = None,
        num_ctx: Optional[int] = None,
        output_schema: Optional[dict] = None,
        ignore_circuit: bool = False,
    ) -> str:
        output_format: Any = output_schema if output_schema is not None else ("json" if json_mode else None)
        data = await self._request_chat(
            messages,
            temperature=temperature,
            num_predict=int(num_predict or self.num_predict),
            num_ctx=num_ctx,
            output_format=output_format,
            ignore_circuit=ignore_circuit,
        )
        return self._strip_thinking(str(data.get("message", {}).get("content", "")))

    async def tool_route(self, messages: List[dict], tools: list[dict]) -> tuple[list[dict], str]:
        data = await self._request_chat(
            messages,
            temperature=0,
            num_predict=min(192, self.num_predict),
            tools=tools,
        )
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        return tool_calls, self._strip_thinking(str(message.get("content", "")))

    async def health_check(self, attempts: int = 3) -> tuple[bool, str]:
        self.circuit_open_until = 0.0
        delays = [0, 2, 5]
        last_error = "неизвестная ошибка"
        for attempt in range(1, max(1, attempts) + 1):
            delay = delays[min(attempt - 1, len(delays) - 1)]
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self.chat(
                    [
                        {"role": "system", "content": "Верни только запрошенные символы без пояснений."},
                        {"role": "user", "content": "Напиши только 1234"},
                    ],
                    temperature=0,
                    num_predict=8,
                    ignore_circuit=True,
                )
                normalized = re.sub(r"\s+", "", response)
                if normalized == "1234":
                    return True, f"Ollama и модель {self.model} работают"
                last_error = f"ожидалось 1234, получено: {response[:80]!r}"
            except Exception as exc:
                last_error = str(exc)
            logger.warning(f"Проверка Ollama {attempt}/{attempts}: {last_error}")
        return False, last_error


class AIBlock:
    def __init__(self, block_path: Path):
        self.path = block_path
        self.name = block_path.name
        self.module = None
        self.functions: Dict[str, Callable] = {}
        self.load()

    def load(self) -> None:
        import importlib.util
        import sys

        block_file = self.path / "block.py"
        if not block_file.exists():
            return
        module_name = f"userbot.ai.blocks.{self.name}.block"
        spec = importlib.util.spec_from_file_location(module_name, block_file)
        if not spec or not spec.loader:
            raise ImportError(f"Не удалось создать spec для {block_file}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        self.module = module
        for attr_name in dir(module):
            func = getattr(module, attr_name)
            if callable(func) and getattr(func, "_ai_function", False):
                meta: ActionMeta = func._ai_meta
                self.functions[meta.name] = func
        logger.info(f"AI-блок {self.name}: загружено действий {len(self.functions)}")

    async def execute(
        self,
        name: str,
        params: dict,
        *,
        client: Any,
        message: Any,
        brain: Any = None,
    ) -> Any:
        func = self.functions[name]
        signature = inspect.signature(func)
        kwargs = {key: value for key, value in params.items() if key in signature.parameters}
        if "client" in signature.parameters:
            kwargs["client"] = client
        if "message" in signature.parameters:
            kwargs["message"] = message
        if "brain" in signature.parameters:
            kwargs["brain"] = brain
        result = func(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


class AIBrain:
    def __init__(
        self,
        ollama_url: str,
        model: str,
        base_dir: str,
        *,
        owner_id: Optional[int] = None,
        timeout: int = 120,
        max_queue: int = 20,
        num_ctx: int = 2048,
        num_predict: int = 256,
        keep_alive: str = "30m",
        history_turns: int = 6,
        user_requests_per_minute: int = 10,
        retry_attempts: int = 1,
        circuit_threshold: int = 3,
        circuit_seconds: int = 30,
    ):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.base_dir / "settings.json"
        self.audit_file = self.base_dir / "actions_audit.jsonl"
        self.blocks_dir = self.base_dir / "blocks"
        self.blocks_dir.mkdir(parents=True, exist_ok=True)
        self.owner_id = owner_id
        self.settings = self._load_settings()
        self._autoreply_versions: dict[tuple[str, Any], int] = defaultdict(int)
        self.blocks: Dict[str, AIBlock] = {}
        self.actions: Dict[str, tuple[AIBlock, Callable, ActionMeta]] = {}
        self.pending: Dict[str, PendingAction | PendingPlan] = {}
        self.histories: Dict[int, deque[dict]] = defaultdict(lambda: deque(maxlen=history_turns * 2))
        self.conversation_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.request_times: Dict[int, deque[float]] = defaultdict(deque)
        self.requests_per_minute = max(1, int(user_requests_per_minute))
        self.max_input_chars = 6000
        self.max_context_chars = max(2500, int(num_ctx) * 3)
        self.system_prompt = self._load_system_prompt()
        self.ollama = OllamaClient(
            ollama_url,
            model,
            timeout=timeout,
            parallel=1,
            max_queue=max_queue,
            keep_alive=keep_alive,
            num_ctx=num_ctx,
            num_predict=num_predict,
            retry_attempts=retry_attempts,
            circuit_threshold=circuit_threshold,
            circuit_seconds=circuit_seconds,
        )
        self.load_blocks()
        logger.info(f"AI Brain готов. Модель: {model}; действий: {len(self.actions)}")

    async def close(self) -> None:
        await self.ollama.close()

    async def health_check(self) -> tuple[bool, str]:
        return await self.ollama.health_check(3)

    def set_owner_id(self, owner_id: int) -> None:
        self.owner_id = int(owner_id)

    @staticmethod
    def _default_settings() -> dict:
        def profile():
            return {
                "enabled": False,
                "all": False,
                "users": [],
                "chats": [],
                "contacts": False,
                "non_contacts": False,
                "excluded_users": [],
                "excluded_chats": [],
                "excluded_contacts": False,
                "excluded_non_contacts": False,
            }
        return {
            "answer": {"all": False, "users": []},
            "bypass": {"all": False, "users": []},
            "autoreply": {
                "persona": profile(),
                "assistant": profile(),
            },
        }

    def _load_settings(self) -> dict:
        default = self._default_settings()
        try:
            loaded = json.loads(self.settings_file.read_text(encoding="utf-8"))
            for section in ("answer", "bypass"):
                value = loaded.get(section, {})
                default[section]["all"] = bool(value.get("all", False))
                default[section]["users"] = [int(x) for x in value.get("users", [])]
            autoreply = loaded.get("autoreply", {})
            for mode in ("persona", "assistant"):
                value = autoreply.get(mode, {}) if isinstance(autoreply, dict) else {}
                profile = default["autoreply"][mode]
                for key in ("enabled", "all", "contacts", "non_contacts", "excluded_contacts", "excluded_non_contacts"):
                    profile[key] = bool(value.get(key, False))
                for key in ("users", "chats", "excluded_users", "excluded_chats"):
                    profile[key] = list(dict.fromkeys(int(x) for x in value.get(key, [])))
        except (OSError, ValueError, TypeError):
            pass
        return default

    def _save_settings(self) -> None:
        temp = self.settings_file.with_suffix(".tmp")
        temp.write_text(
            json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(self.settings_file)

    def _load_system_prompt(self) -> str:
        path = self.base_dir / "system_prompt.txt"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return (
            "Ты Фауст, краткий русскоязычный помощник внутри Telegram. "
            "Отвечай полезно и без выдуманных фактов. Не утверждай, что действие выполнено, "
            "если его не выполнил код юзербота."
        )

    def load_blocks(self) -> None:
        self.blocks.clear()
        self.actions.clear()
        for path in sorted(self.blocks_dir.iterdir()):
            if not path.is_dir() or path.name.startswith("_"):
                continue
            try:
                block = AIBlock(path)
                if not block.functions:
                    continue
                self.blocks[block.name] = block
                for name, func in block.functions.items():
                    if name in self.actions:
                        raise ValueError(f"Действие {name} объявлено повторно")
                    self.actions[name] = (block, func, func._ai_meta)
            except Exception:
                logger.exception(f"Не удалось загрузить AI-блок {path.name}")

    def reload(self) -> int:
        self.system_prompt = self._load_system_prompt()
        self.load_blocks()
        return len(self.actions)

    def can_answer(self, user_id: int) -> bool:
        section = self.settings["answer"]
        return bool(section["all"] or int(user_id) in section["users"])

    def can_bypass(self, user_id: int) -> bool:
        if self.owner_id and int(user_id) == self.owner_id:
            return True
        section = self.settings["bypass"]
        return bool(section["all"] or int(user_id) in section["users"])

    def _set_access(self, section: str, user_id: Optional[int], enabled: bool) -> None:
        data = self.settings[section]
        if user_id is None:
            data["all"] = bool(enabled)
        elif enabled and int(user_id) not in data["users"]:
            data["users"].append(int(user_id))
        elif not enabled and int(user_id) in data["users"]:
            data["users"].remove(int(user_id))
        self._save_settings()

    def set_answer(self, user_id: Optional[int], enabled: bool) -> None:
        self._invalidate_autoreply("all" if user_id is None else "users", user_id)
        if enabled:
            profile = self.settings["autoreply"]["assistant"]
            if user_id is not None and int(user_id) in profile["excluded_users"]:
                profile["excluded_users"].remove(int(user_id))
        self._set_access("answer", user_id, enabled)

    def set_bypass(self, user_id: Optional[int], enabled: bool) -> None:
        self._set_access("bypass", user_id, enabled)

    @staticmethod
    def _autoreply_mode_name(mode: str) -> str:
        normalized = str(mode or "").strip().lower()
        aliases = {
            "persona": "persona", "owner": "persona", "user": "persona",
            "мой": "persona", "от моего имени": "persona", "как я": "persona",
            "assistant": "assistant", "ai": "assistant", "ассистент": "assistant",
            "ии": "assistant",
        }
        if normalized not in aliases:
            raise ValueError("Режим должен быть persona или assistant")
        return aliases[normalized]

    def configure_autoreply(
        self,
        mode: str,
        scope: str,
        enabled: bool,
        target: Optional[Any] = None,
    ) -> str:
        mode = self._autoreply_mode_name(mode)
        scope = str(scope or "all").strip().lower().replace("-", "_")
        scope_aliases = {
            "all": "all", "все": "all", "всем": "all",
            "user": "users", "users": "users", "пользователь": "users",
            "chat": "chats", "group": "chats", "чаты": "chats", "группа": "chats",
            "contacts": "contacts", "контакты": "contacts",
            "noncontacts": "non_contacts", "non_contacts": "non_contacts",
            "не контакты": "non_contacts", "неконтакты": "non_contacts",
        }
        scope_key = scope_aliases.get(scope)
        if not scope_key:
            raise ValueError("Область: all, user, chat, contacts или noncontacts")
        profile = self.settings["autoreply"][mode]
        entity_id = None
        if scope_key in {"users", "chats"}:
            if target in (None, ""):
                raise ValueError("Для пользователя или чата нужен числовой Telegram ID")
            entity_id = int(target)
            if not entity_id or (scope_key == "users" and entity_id < 0):
                raise ValueError("Нужен корректный Telegram ID; ID пользователя должен быть положительным")
            excluded = profile["excluded_" + scope_key]
            if enabled and entity_id in excluded:
                excluded.remove(entity_id)
            if not enabled and entity_id not in excluded:
                excluded.append(entity_id)
            if enabled and entity_id not in profile[scope_key]:
                profile[scope_key].append(entity_id)
            elif not enabled and entity_id in profile[scope_key]:
                profile[scope_key].remove(entity_id)
        elif scope_key == "all" and not enabled:
            for key in ("all", "contacts", "non_contacts"):
                profile[key] = False
            profile["users"] = []
            profile["chats"] = []
            if mode == "assistant":
                self.settings["answer"] = {"all": False, "users": []}
        else:
            profile[scope_key] = bool(enabled)
            if scope_key in {"contacts", "non_contacts"}:
                profile["excluded_" + scope_key] = not enabled
        profile["enabled"] = any(
            bool(profile[key])
            for key in ("all", "users", "chats", "contacts", "non_contacts")
        )
        self._save_settings()
        self._invalidate_autoreply(scope_key, entity_id)
        mode_title = "ответы от моего имени" if mode == "persona" else "AI-ассистент"
        target_text = f" {int(target)}" if target not in (None, "") else ""
        scope_title = {
            "all": "всех",
            "users": "пользователя",
            "chats": "чата",
            "contacts": "контактов",
            "non_contacts": "пользователей не из контактов",
        }[scope_key]
        return (
            f"Готово: режим «{mode_title}» {'включён' if enabled else 'выключен'} для "
            f"{scope_title}{target_text}."
        )

    def _invalidate_autoreply(self, scope: str, target: Any = None) -> None:
        self._autoreply_versions[(scope, target)] += 1

    def autoreply_ticket(self, user_id: int, chat_id: int, is_contact: bool) -> tuple[int, ...]:
        return tuple(self._autoreply_versions[key] for key in (
            ("all", None), ("users", int(user_id)), ("chats", int(chat_id)),
            ("contacts" if is_contact else "non_contacts", None),
        ))

    @staticmethod
    def _autoreply_excluded(profile: dict, user_id: int, chat_id: int, is_contact: bool) -> bool:
        if chat_id in profile.get("excluded_chats", []) or user_id in profile.get("excluded_users", []):
            return True
        if chat_id in profile.get("chats", []) or user_id in profile.get("users", []):
            return False
        return bool(profile.get("excluded_contacts" if is_contact else "excluded_non_contacts"))

    def autoreply_mode(self, user_id: int, chat_id: int, is_contact: bool) -> Optional[str]:
        user_id = int(user_id)
        chat_id = int(chat_id)
        choices = []
        for priority, mode in ((1, "persona"), (0, "assistant")):
            profile = self.settings["autoreply"][mode]
            if not profile.get("enabled") or self._autoreply_excluded(profile, user_id, chat_id, is_contact):
                continue
            score = 3 if chat_id in profile["chats"] or user_id in profile["users"] else (
                2 if profile["contacts" if is_contact else "non_contacts"] else (1 if profile["all"] else 0)
            )
            if score:
                choices.append((score, priority, mode))
        return max(choices)[2] if choices else None

    def resolve_autoreply_mode(
        self, user_id: int, chat_id: int, is_contact: bool, *, is_private: bool,
        legacy_private_only: bool = True,
    ) -> Optional[str]:
        mode = self.autoreply_mode(user_id, chat_id, is_contact)
        if mode is not None:
            return mode
        if (is_private or not legacy_private_only) and self.can_answer(user_id):
            if not self._autoreply_excluded(
                self.settings["autoreply"]["assistant"], int(user_id), int(chat_id), is_contact,
            ):
                return "assistant"
        return None

    def select_autoreply(self, mode: Optional[str], scope: str, target: Optional[int] = None) -> str:
        if scope not in {"all", "chat"}:
            raise ValueError("Панель поддерживает all или chat")
        if mode is not None:
            mode = self._autoreply_mode_name(mode)
        if scope == "chat" and (target is None or not int(target)):
            raise ValueError("Открой .panel именно в нужном чате")
        for item in ("persona", "assistant"):
            self.configure_autoreply(item, scope, item == mode, target)
        location = "во всех чатах" if scope == "all" else f"в чате {int(target)}"
        if mode is None:
            return f"Автоответ выключен {location}. Ожидающие ответы для этой области отменены."
        title = "От моего имени" if mode == "persona" else "AI-ассистент"
        suffix = " Исключения для отдельных чатов и пользователей сохранены." if scope == "all" else ""
        return f"Автоответ {location}: {title}.{suffix}"

    def autoreply_status_text(self) -> str:
        lines = ["Автоответчик: индивидуальные правила важнее общих; при равенстве — persona."]
        for mode, title in (("persona", "От моего имени"), ("assistant", "AI-ассистент")):
            profile = self.settings["autoreply"][mode]
            scopes = []
            if profile["all"]:
                scopes.append("все")
            if profile["users"]:
                scopes.append("пользователи: " + ", ".join(map(str, profile["users"])))
            if profile["chats"]:
                scopes.append("чаты: " + ", ".join(map(str, profile["chats"])))
            if profile["contacts"]:
                scopes.append("контакты")
            if profile["non_contacts"]:
                scopes.append("не контакты")
            lines.append(f"{title}: " + ("; ".join(scopes) if scopes else "выключен"))
            for key, label in (("excluded_chats", "Не отвечать в чатах"), ("excluded_users", "Не отвечать пользователям")):
                if profile[key]:
                    lines.append(f"  {label}: " + ", ".join(map(str, profile[key])))
            if profile["excluded_contacts"]:
                lines.append("  Контакты исключены")
            if profile["excluded_non_contacts"]:
                lines.append("  Не контакты исключены")
        if self.settings["answer"]["all"] or self.settings["answer"]["users"]:
            lines.append("Включены также старые правила .answer (с учётом исключений AI-ассистента).")
        return "\n".join(lines)

    def autoreply_scope_status(self, scope: str, chat_id: Optional[int] = None) -> str:
        if scope == "all":
            return self.autoreply_status_text()
        if chat_id is None:
            raise ValueError("Чат панели не определён")
        states = []
        for mode, title in (("persona", "От моего имени"), ("assistant", "AI-ассистент")):
            profile = self.settings["autoreply"][mode]
            if int(chat_id) in profile["excluded_chats"]:
                value = "ВЫКЛ для этого чата"
            elif int(chat_id) in profile["chats"]:
                value = "ВКЛ для этого чата"
            else:
                value = "по общим правилам"
            states.append(f"{title}: {value}")
        return f"Чат {int(chat_id)}\n" + "\n".join(states)

    def forget(self, conversation_id: int) -> None:
        self.histories.pop(int(conversation_id), None)

    def _check_rate_limit(self, user_id: int) -> None:
        now = time.monotonic()
        bucket = self.request_times[int(user_id)]
        while bucket and now - bucket[0] >= 60:
            bucket.popleft()
        if len(bucket) >= self.requests_per_minute:
            raise OllamaBusyError(
                f"Слишком много запросов: максимум {self.requests_per_minute} в минуту"
            )
        bucket.append(now)

    @staticmethod
    def _normalized(text: str) -> str:
        return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()

    @staticmethod
    def _extract_params(text: str, meta: ActionMeta, matched_trigger: str = "") -> dict:
        result: dict = {}
        quoted = re.findall(r"[\"«](.*?)[\"»]", text)
        username = re.search(r"@[A-Za-z0-9_]{5,}", text)
        chat_id = re.search(r"(?<!\d)-?100\d{6,}(?!\d)", text)
        entity_id = re.search(r"(?<!\d)-?\d{5,15}(?!\d)", text)
        minutes = re.search(r"(\d{1,5})\s*(?:минут(?:у|ы)?|мин\.?)(?!\w)", text, re.I)
        count = re.search(r"(\d{1,4})\s*(?:сообщени(?:е|я|й)|шт\.?)", text, re.I)
        alias_match = re.search(
            r"(?:с|под)\s+(?:названием|именем|ярлыком)\s+(.+?)"
            r"(?=\s+(?:и\s+)?(?:скинь|отправь|перешли|пошли|удали)\b|[.!?]*$)",
            text,
            re.I,
        )
        alias_value = (
            alias_match.group(1).strip(" \t\r\n\"'«».,!?")
            if alias_match
            else ""
        )
        lowered = text.lower().replace("ё", "е")
        for spec in meta.params:
            name = spec.get("name")
            if name in {"target", "chat"}:
                if username:
                    result[name] = username.group(0)
                elif chat_id:
                    result[name] = int(chat_id.group(0))
                elif entity_id:
                    result[name] = int(entity_id.group(0))
            elif name in {"minutes", "delay_minutes"} and minutes:
                result[name] = int(minutes.group(1))
            elif name in {"limit", "max_count"} and count:
                result[name] = int(count.group(1))
            elif name == "alias" and alias_value:
                result[name] = alias_value
            elif name in {"query", "text"} and quoted:
                result[name] = quoted[-1]
            elif name in {"query", "text"} and matched_trigger:
                remainder = re.sub(re.escape(matched_trigger), "", text, count=1, flags=re.I)
                remainder = re.sub(r"@[A-Za-z0-9_]{5,}", "", remainder, count=1)
                remainder = remainder.lstrip(" :-—")
                if remainder:
                    result[name] = remainder
            elif name == "enabled":
                if re.search(r"\b(?:выключ|отключ|off)\w*", lowered):
                    result[name] = False
                elif re.search(r"\b(?:включ|активир|on)\w*", lowered):
                    result[name] = True
            elif name == "mode":
                if "ассистент" in lowered or re.search(r"\bai\b|\bии\b", lowered):
                    result[name] = "assistant"
                elif "от моего имени" in lowered or "как я" in lowered or "persona" in lowered:
                    result[name] = "persona"
            elif name == "scope":
                if "не контакт" in lowered or "неконтакт" in lowered:
                    result[name] = "noncontacts"
                elif "контакт" in lowered:
                    result[name] = "contacts"
                elif "групп" in lowered or "чат" in lowered:
                    result[name] = "chat"
                elif "пользовател" in lowered or "юзер" in lowered:
                    result[name] = "user"
                elif "для всех" in lowered or "всем" in lowered:
                    result[name] = "all"
        return result

    def _catalog(self) -> list[dict]:
        return [
            {
                "name": meta.name,
                "description": meta.description,
                "params": list(meta.params),
                "risk": meta.risk,
            }
            for _, _, meta in self.actions.values()
        ]

    def _router_action_names(self, text: str, limit: int = 3) -> list[str]:
        """Rank close actions only for clarification suggestions, never for routing."""
        request = self._normalized(text)
        request_tokens = {
            token[:6] for token in re.findall(r"[a-zа-яё0-9_]{4,}", request)
        }
        ranked = []
        for name, (_, _, meta) in self.actions.items():
            haystack = " ".join((name, meta.description, *meta.triggers)).lower().replace("ё", "е")
            action_tokens = {
                token[:6] for token in re.findall(r"[a-zа-яё0-9_]{4,}", haystack)
            }
            overlap = len(request_tokens & action_tokens)
            phrase_bonus = max(
                (len(trigger) for trigger in meta.triggers if self._normalized(trigger) in request),
                default=0,
            )
            ranked.append((phrase_bonus * 10 + overlap, name))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        selected = [name for score, name in ranked if score > 0][:limit]
        defaults = (
            "chat_info", "user_info", "recent_messages", "list_dialogs",
            "send_message", "ai_autoreply_status",
        )
        for name in defaults:
            if name in self.actions and name not in selected and len(selected) < limit:
                selected.append(name)
        if not selected:
            selected = [name for _, name in ranked[:limit]]
        return selected

    def _router_catalog(self, names: Optional[Iterable[str]] = None) -> str:
        """Compact action catalog for the neural router."""
        risk_codes = {"read": "R", "write": "W", "destructive": "D"}
        lines = []
        allowed = set(names) if names is not None else None
        for _, _, meta in sorted(self.actions.values(), key=lambda item: item[2].name):
            if allowed is not None and meta.name not in allowed:
                continue
            params = ",".join(
                f"{spec.get('name')}{'' if spec.get('required') else '?'}"
                for spec in meta.params
            )
            lines.append(
                f"[{risk_codes.get(meta.risk, 'R')}] {meta.name}({params}): {meta.description}"
            )
        return "\n".join(lines)

    def _action_examples(self, names: Iterable[str], limit: int = 3) -> list[str]:
        custom = {
            "download_replied_file": ".фауст сохрани его с названием <ярлык>",
            "send_saved_file": ".фауст скинь файл <ярлык> <ID, @username или имя>",
            "set_ai_autoreply": ".фауст включи автоответ от моего имени для пользователя <ID>",
            "schedule_message": ".фауст отправь <имя> сообщение \"<текст>\" через <число> минут",
        }
        placeholders = {
            "alias": "<ярлык>",
            "target": "<ID, @username или имя>",
            "text": "\"<текст>\"",
            "query": "\"<что искать>\"",
            "delay_minutes": "<число минут>",
            "mode": "<persona или assistant>",
            "scope": "<all, user, chat, contacts или noncontacts>",
            "enabled": "<включить или выключить>",
        }
        result = []
        for name in names:
            if name not in self.actions:
                continue
            meta = self.actions[name][2]
            example = custom.get(name)
            if not example:
                trigger = meta.triggers[0] if meta.triggers else meta.description.lower()
                required = [
                    placeholders.get(str(spec.get("name")), f"<{spec.get('name')}>")
                    for spec in meta.params
                    if spec.get("required")
                ]
                example = ".фауст " + trigger + (" " + " ".join(required) if required else "")
            if example not in result:
                result.append(example)
            if len(result) >= limit:
                break
        return result

    def _uncertain_response(
        self,
        text: str,
        clarification: str,
        suggestions: Any = None,
    ) -> str:
        clean_suggestions = []
        if isinstance(suggestions, list):
            for item in suggestions[:3]:
                value = str(item or "").strip().strip("`\"'")
                if not value:
                    continue
                if not value.lower().startswith((".фауст", ".faust")):
                    value = ".фауст " + value
                clean_suggestions.append(value)
        if not clean_suggestions:
            clean_suggestions = self._action_examples(self._router_action_names(text, 3))
        lines = [
            "Я не уверен, что правильно понял нужное действие.",
            f"Уточни: {clarification or 'что именно нужно сделать?'}",
        ]
        if clean_suggestions:
            lines.append("\nНаиболее похожие варианты:")
            lines.extend(f"{index}. {value}" for index, value in enumerate(clean_suggestions, 1))
        return "\n".join(lines)

    def _missing_params_response(self, meta: ActionMeta, error: Exception) -> str:
        examples = self._action_examples([meta.name])
        lines = [
            f"Я понял действие «{meta.description.lower()}», но данных пока недостаточно.",
            f"Что нужно уточнить: {error}",
        ]
        if examples:
            lines.append(f"\nПопробуй так:\n{examples[0]}")
        return "\n".join(lines)

    @staticmethod
    def _parameter_schema(spec: dict) -> dict:
        kind = str(spec.get("type", "string"))
        schema: dict = {
            "type": kind if kind in {"string", "integer", "number", "boolean", "array"} else "string"
        }
        if spec.get("description"):
            schema["description"] = str(spec["description"])
        if "default" in spec:
            schema["default"] = spec["default"]
        if "min" in spec:
            schema["minimum"] = spec["min"]
        if "max" in spec:
            schema["maximum"] = spec["max"]
        if "max_length" in spec:
            schema["maxLength"] = spec["max_length"]
        if spec.get("enum"):
            schema["enum"] = list(spec["enum"])
        return schema

    def _tool_catalog(self) -> list[dict]:
        tools = []
        for _, _, meta in self.actions.values():
            properties = {
                str(spec["name"]): self._parameter_schema(spec)
                for spec in meta.params
            }
            required = [str(spec["name"]) for spec in meta.params if spec.get("required")]
            parameters: dict = {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            }
            if required:
                parameters["required"] = required
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": meta.name,
                        "description": (
                            f"{meta.description}. Уровень риска: {meta.risk}. "
                            "Вызывай только при явной просьбе выполнить действие."
                        ),
                        "parameters": parameters,
                    },
                }
            )
        return tools

    @staticmethod
    def _parse_json(text: str) -> dict:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                return {}
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}

    @classmethod
    def _literal_target(cls, value: Any, text: str) -> bool:
        candidate = cls._normalized(str(value or "").strip().lstrip("@"))
        source = cls._normalized(text).lstrip("@")
        return bool(candidate and len(candidate) >= 3 and candidate in source)

    @staticmethod
    async def _telegram_context(message: Any) -> str:
        if message is None:
            return "Команда вызвана без доступного Telegram-контекста."
        lines = [
            f"chat_id={getattr(message, 'chat_id', None)}",
            f"private={bool(getattr(message, 'is_private', False))}",
            f"group={bool(getattr(message, 'is_group', False))}",
            f"reply={bool(getattr(message, 'is_reply', False))}",
        ]
        if getattr(message, "is_reply", False):
            try:
                reply = await message.get_reply_message()
            except Exception as exc:
                lines.append(f"reply_error={type(exc).__name__}")
                reply = None
            if reply is not None:
                file = getattr(reply, "file", None)
                lines.extend(
                    [
                        f"reply_message_id={getattr(reply, 'id', None)}",
                        f"reply_sender_id={getattr(reply, 'sender_id', None)}",
                        f"reply_has_media={bool(getattr(reply, 'media', None) or file)}",
                        f"reply_file_name={getattr(file, 'name', None)}",
                        f"reply_mime_type={getattr(file, 'mime_type', None)}",
                        f"reply_text={str(getattr(reply, 'raw_text', '') or '')[:500]!r}",
                    ]
                )
        return "\n".join(lines)

    async def _ai_candidate(
        self,
        text: str,
        message: Any = None,
    ) -> Optional[ActionCandidate | ActionPlanCandidate | str]:
        allowed_actions = sorted(self.actions)
        telegram_context = await self._telegram_context(message)
        step_schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": sorted(allowed_actions)},
                "params": {"type": "object"},
            },
            "required": ["action", "params"],
            "additionalProperties": False,
        }
        schema = {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": step_schema,
                    "maxItems": 4,
                },
                "answer": {"type": "string"},
                "uncertain": {"type": "boolean"},
                "clarification": {"type": "string"},
                "suggestions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 3,
                },
            },
            "required": ["steps", "answer", "uncertain", "clarification", "suggestions"],
            "additionalProperties": False,
        }
        prompt = (
            "Пойми запрос по смыслу, даже если он разговорный, с местоимениями, опечатками или "
            "не совпадает с примерами. Составь план из 1-4 Telegram-действий или ответь сам. R получает реальные данные; "
            "Слова «это», «его», «тот файл» и похожие считай ссылкой на сообщение в реплае, "
            "если reply=true; при reply_has_media=true это реальный доступный файл/медиа. "
            "W/D выбирай только по явной просьбе. Вопросы о текущем чате, собеседнике, "
            "сообщениях и диалогах требуют R. Для нескольких последовательных просьб верни "
            "несколько steps в порядке выполнения. Обычный разговор и отрицание: steps=[], "
            "answer=короткий полноценный ответ. При действиях answer='' и только нужные params. "
            "target может быть @username, числовым Telegram ID или буквально указанным именем; "
            "копируй его из запроса без изменений и никогда не придумывай. В цепочке "
            "download_replied_file -> send_saved_file используй один и тот же alias. "
            "Если запрос похож на действие, но намерение неоднозначно или обязательных данных не хватает, "
            "не угадывай и не вызывай действие: steps=[], uncertain=true, clarification=короткий вопрос, "
            "suggestions=до 3 наиболее похожих готовых примеров команд с префиксом .фауст. "
            "Для обычного разговора uncertain=false, clarification='', suggestions=[].\n"
            f"КОНТЕКСТ TELEGRAM:\n{telegram_context}\n"
            f"ПОЛНЫЙ КАТАЛОГ ДЕЙСТВИЙ:\n{self._router_catalog()}\n"
            f"ЗАПРОС: {text}"
        )
        messages = [
            {"role": "system", "content": "Верни только JSON по заданной схеме."},
            {"role": "user", "content": prompt},
        ]
        try:
            response = await self.ollama.chat(
                messages,
                temperature=0,
                num_predict=256,
                num_ctx=2048,
                output_schema=schema,
            )
        except OllamaError as schema_error:
            if "HTTP 400" not in str(schema_error):
                raise
            logger.warning(f"JSON Schema недоступна, используется JSON mode: {schema_error}")
            response = await self.ollama.chat(
                messages,
                temperature=0,
                num_predict=256,
                num_ctx=2048,
                json_mode=True,
            )
        data = self._parse_json(response)
        raw_steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        if not raw_steps and data.get("action") not in (None, "none"):
            raw_steps = [{"action": data.get("action"), "params": data.get("params", {})}]
        if not raw_steps:
            answer = str(data.get("answer") or "").strip()
            if bool(data.get("uncertain")):
                return self._uncertain_response(
                    text,
                    str(data.get("clarification") or answer).strip(),
                    data.get("suggestions"),
                )
            if not answer:
                return self._uncertain_response(text, "Я не до конца понял, что именно нужно сделать.")
            return answer or None
        candidates: list[ActionCandidate] = []
        for raw_step in raw_steps[:4]:
            if not isinstance(raw_step, dict):
                continue
            name = str(raw_step.get("action") or "")
            if name not in self.actions:
                continue
            params = raw_step.get("params") if isinstance(raw_step.get("params"), dict) else {}
            params = dict(params)
            meta = self.actions[name][2]
            explicit = self._extract_params(text, meta)
            for parameter in ("alias", "enabled", "mode", "scope", "delay_minutes", "minutes", "limit", "max_count"):
                if parameter in explicit:
                    params[parameter] = explicit[parameter]
            for parameter in ("target", "chat"):
                if parameter in explicit:
                    params[parameter] = explicit[parameter]
                elif parameter in params and not self._literal_target(params[parameter], text):
                    params.pop(parameter, None)
            candidates.append(
                ActionCandidate(name=name, params=params, confidence=1.0, source="neural_router")
            )
        if not candidates:
            return self._uncertain_response(
                text,
                str(data.get("clarification") or data.get("answer") or "Не удалось однозначно выбрать действие.").strip(),
                data.get("suggestions"),
            )
        if len(candidates) == 1:
            return candidates[0]
        return ActionPlanCandidate(candidates)

    def _chat_messages(self, conversation: int, text: str) -> list[dict]:
        system = {"role": "system", "content": self.system_prompt}
        user = {"role": "user", "content": text}
        budget = max(500, self.max_context_chars - len(system["content"]) - len(text))
        selected: list[dict] = []
        used = 0
        for item in reversed(list(self.histories[conversation])):
            size = len(str(item.get("content", "")))
            if selected and used + size > budget:
                break
            if size > budget and not selected:
                trimmed = dict(item)
                trimmed["content"] = str(item.get("content", ""))[-budget:]
                selected.append(trimmed)
                break
            selected.append(item)
            used += size
        selected.reverse()
        return [system, *selected, user]

    @staticmethod
    def _coerce(value: Any, kind: str) -> Any:
        if kind == "integer":
            if isinstance(value, bool):
                raise ValueError("ожидалось число")
            return int(value)
        if kind == "number":
            return float(value)
        if kind == "boolean":
            if isinstance(value, bool):
                return value
            if str(value).lower() in {"true", "1", "yes", "да", "on"}:
                return True
            if str(value).lower() in {"false", "0", "no", "нет", "off"}:
                return False
            raise ValueError("ожидалось логическое значение")
        return str(value).strip()

    def _validate_params(self, meta: ActionMeta, params: dict) -> dict:
        clean: dict = {}
        for spec in meta.params:
            name = str(spec["name"])
            if name not in params or params[name] in (None, ""):
                if "default" in spec:
                    clean[name] = spec["default"]
                elif spec.get("required"):
                    raise ValueError(f"Не указан обязательный параметр: {name}")
                continue
            value = self._coerce(params[name], str(spec.get("type", "string")))
            if isinstance(value, (int, float)):
                if "min" in spec and value < spec["min"]:
                    raise ValueError(f"{name} должен быть не меньше {spec['min']}")
                if "max" in spec and value > spec["max"]:
                    raise ValueError(f"{name} должен быть не больше {spec['max']}")
            if isinstance(value, str) and len(value) > int(spec.get("max_length", 4000)):
                raise ValueError(f"Параметр {name} слишком длинный")
            if spec.get("enum") and value not in spec["enum"]:
                raise ValueError(f"{name} должен быть одним из: {', '.join(map(str, spec['enum']))}")
            clean[name] = value
        return clean

    @staticmethod
    def _human_params(params: dict) -> str:
        labels = {
            "target": "Кому/куда",
            "chat": "Чат",
            "text": "Текст",
            "query": "Поиск",
            "limit": "Количество",
            "max_count": "Максимум",
            "minutes": "Минуты",
            "alias": "Ярлык",
            "language": "Язык",
            "emoji": "Реакция",
            "schedule": "Время",
            "mode": "Режим",
            "scope": "Область",
            "enabled": "Состояние",
        }
        if not params:
            return "текущий чат или сообщение в реплае"
        values = []
        for key, value in params.items():
            title = labels.get(key, key.replace("_", " ").capitalize())
            if isinstance(value, bool):
                value = "включено" if value else "выключено"
            values.append(f"{title}: {value}")
        return "; ".join(values)

    @classmethod
    def _preview(cls, meta: ActionMeta, params: dict) -> str:
        return f"{meta.description}. {cls._human_params(params)}"

    async def generate_text(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: float = 0.25,
        num_predict: Optional[int] = None,
        num_ctx: Optional[int] = None,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": str(prompt)})
        return await self.ollama.chat(
            messages,
            temperature=temperature,
            num_predict=num_predict,
            num_ctx=num_ctx,
        )

    async def generate_messages(
        self, messages: List[dict], *, temperature: float = 0.3,
        num_predict: Optional[int] = None, num_ctx: Optional[int] = None,
    ) -> str:
        return await self.ollama.chat(
            messages, temperature=temperature, num_predict=num_predict, num_ctx=num_ctx,
        )

    async def _prepare_action(
        self,
        user_id: int,
        candidate: ActionCandidate,
        *,
        client: Any,
        message: Any,
    ) -> str:
        _, _, meta = self.actions[candidate.name]
        if not self.can_bypass(user_id):
            return "Нет прав на выполнение AI-действий."
        if meta.owner_only and (not self.owner_id or int(user_id) != self.owner_id):
            return "Это действие доступно только владельцу юзербота."
        try:
            params = self._validate_params(meta, candidate.params)
        except (ValueError, TypeError) as exc:
            return self._missing_params_response(meta, exc)

        if meta.confirmation:
            token = secrets.token_hex(2).upper()
            now = time.monotonic()
            self.pending[token] = PendingAction(
                token=token,
                user_id=int(user_id),
                action_name=meta.name,
                params=params,
                created_at=now,
                expires_at=now + 120,
                client=client,
                message=message,
            )
            return (
                "Я понял задачу. Требуется подтверждение:\n"
                f"{self._preview(meta, params)}\n\n"
                f"Чтобы выполнить, отправь .confirm {token} в течение 2 минут.\n"
                "Если передумал: .cancel"
            )
        return await self._execute(meta.name, params, user_id, client=client, message=message)

    async def _prepare_plan(
        self,
        user_id: int,
        plan: ActionPlanCandidate,
        *,
        client: Any,
        message: Any,
    ) -> str:
        if not plan.steps:
            return "Не удалось составить план действий."
        if len(plan.steps) == 1:
            return await self._prepare_action(user_id, plan.steps[0], client=client, message=message)
        if not self.can_bypass(user_id):
            return "Нет прав на выполнение AI-действий."
        prepared: list[tuple[str, dict]] = []
        previews: list[str] = []
        requires_confirmation = False
        chained_alias = None
        for candidate in plan.steps:
            _, _, meta = self.actions[candidate.name]
            if candidate.name == "download_replied_file":
                chained_alias = candidate.params.get("alias")
            elif candidate.name == "send_saved_file" and chained_alias and not candidate.params.get("alias"):
                candidate.params["alias"] = chained_alias
            if meta.owner_only and (not self.owner_id or int(user_id) != self.owner_id):
                return f"Шаг «{meta.description}» доступен только владельцу юзербота."
            try:
                params = self._validate_params(meta, candidate.params)
            except (ValueError, TypeError) as exc:
                return self._missing_params_response(meta, exc)
            prepared.append((meta.name, params))
            previews.append(f"{len(previews) + 1}. {self._preview(meta, params)}")
            requires_confirmation = requires_confirmation or meta.confirmation
        if requires_confirmation:
            token = secrets.token_hex(2).upper()
            now = time.monotonic()
            self.pending[token] = PendingPlan(
                token=token,
                user_id=int(user_id),
                steps=prepared,
                created_at=now,
                expires_at=now + 120,
                client=client,
                message=message,
            )
            return (
                f"Я подготовил план из {len(prepared)} шагов:\n"
                + "\n".join(previews)
                + f"\n\nЧтобы выполнить весь план, отправь .confirm {token} в течение 2 минут.\n"
                "Если передумал: .cancel"
            )
        return await self._execute_plan(prepared, user_id, client=client, message=message)

    async def _execute_plan(
        self,
        steps: list[tuple[str, dict]],
        user_id: int,
        *,
        client: Any,
        message: Any,
    ) -> str:
        results = []
        for index, (action_name, params) in enumerate(steps, 1):
            result = await self._execute(
                action_name,
                params,
                user_id,
                client=client,
                message=message,
            )
            results.append(f"{index}. {result}")
            if result.startswith("Не получилось") or result.startswith("Не удалось"):
                results.append("Следующие шаги остановлены, чтобы не выполнить неполный план.")
                break
        return "План выполнен:\n" + "\n".join(results)

    async def _execute(
        self,
        action_name: str,
        params: dict,
        user_id: int,
        *,
        client: Any,
        message: Any,
    ) -> str:
        block, _, meta = self.actions[action_name]
        started = time.monotonic()
        ok = False
        error = None
        try:
            result = await block.execute(
                action_name,
                params,
                client=client,
                message=message,
                brain=self,
            )
            ok = True
            return str(result)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.exception(f"Ошибка AI-действия {action_name}")
            return f"Не получилось выполнить «{meta.description}»: {exc}"
        finally:
            record = {
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "user_id": int(user_id),
                "action": action_name,
                "risk": meta.risk,
                "params": params,
                "ok": ok,
                "error": error,
                "elapsed": round(time.monotonic() - started, 3),
            }
            try:
                with self.audit_file.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                logger.exception("Не удалось записать аудит действий")

    async def confirm_action(self, user_id: int, token: str, *, client: Any, message: Any) -> str:
        token = token.strip().upper()
        pending = self.pending.get(token)
        if not pending:
            return "Подтверждение не найдено или уже использовано."
        if pending.user_id != int(user_id):
            return "Это подтверждение принадлежит другому пользователю."
        self.pending.pop(token, None)
        if time.monotonic() > pending.expires_at:
            return "Время подтверждения истекло."
        if isinstance(pending, PendingPlan):
            return await self._execute_plan(
                pending.steps,
                user_id,
                client=pending.client or client,
                message=pending.message or message,
            )
        return await self._execute(
            pending.action_name,
            pending.params,
            user_id,
            client=pending.client or client,
            message=pending.message or message,
        )

    def cancel_actions(self, user_id: int) -> int:
        tokens = [token for token, item in self.pending.items() if item.user_id == int(user_id)]
        for token in tokens:
            self.pending.pop(token, None)
        return len(tokens)

    async def process_message(
        self,
        user_id: int,
        username: str,
        text: str,
        is_command: bool,
        client: Any = None,
        message: Any = None,
        chat_id: Optional[int] = None,
    ) -> Optional[str]:
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(text)).strip()
        if not text:
            return "После команды нет текста." if is_command else None
        if len(text) > self.max_input_chars:
            return f"Сообщение слишком длинное. Максимум {self.max_input_chars} символов."

        logger.info(f"AI-запрос от {username} ({user_id}): {text[:100]}")
        rate_slot_used = False
        if is_command and self.actions:
            try:
                self._check_rate_limit(user_id)
                rate_slot_used = True
                candidate = await self._ai_candidate(text, message=message)
            except OllamaError as exc:
                return f"Ошибка AI-маршрутизатора: {exc}"
            if isinstance(candidate, str):
                conversation = int(chat_id if chat_id is not None else user_id)
                self.histories[conversation].append({"role": "user", "content": text})
                self.histories[conversation].append({"role": "assistant", "content": candidate})
                return candidate
            if isinstance(candidate, ActionPlanCandidate):
                return await self._prepare_plan(
                    user_id, candidate, client=client, message=message
                )
            if candidate is not None:
                return await self._prepare_action(
                    user_id, candidate, client=client, message=message
                )
            return self._uncertain_response(
                text,
                "уточни желаемый результат и объект, с которым нужно работать",
            )

        if not is_command and not self.can_answer(user_id):
            return None
        try:
            if not rate_slot_used:
                self._check_rate_limit(user_id)
            conversation = int(chat_id if chat_id is not None else user_id)
            async with self.conversation_locks[conversation]:
                response = await self.ollama.chat(self._chat_messages(conversation, text))
                if not response:
                    return "Ollama вернула пустой ответ."
                self.histories[conversation].append({"role": "user", "content": text})
                self.histories[conversation].append({"role": "assistant", "content": response})
                return response
        except OllamaError as exc:
            logger.warning(f"Ошибка Ollama: {exc}")
            return f"Ollama сейчас недоступна: {exc}"

    async def stats_text(self) -> str:
        stats = self.ollama.last_stats
        lines = [
            f"Модель: {self.ollama.model}",
            "AI-маршрутизатор: эта же модель, короткий JSON-запрос",
            f"Действий загружено: {len(self.actions)}",
            f"Ожидают подтверждения: {len(self.pending)}",
            f"Очередь Ollama: {self.ollama._waiting}; активно: {self.ollama._active}",
            f"Ошибок подряд: {self.ollama.consecutive_failures}",
        ]
        circuit_left = max(0, int(self.ollama.circuit_open_until - time.monotonic()))
        lines.append(f"Circuit breaker: {'открыт на ' + str(circuit_left) + ' сек.' if circuit_left else 'закрыт'}")
        if stats:
            lines.extend(
                [
                    f"Последний ответ: {stats.get('elapsed', 0)} сек.",
                    f"Скорость: {stats.get('tokens_per_second', 0)} токен/сек.",
                ]
            )
        return "\n".join(lines)

    async def list_capabilities(self) -> str:
        if not self.actions:
            return "AI-действия не загружены."
        groups: Dict[str, list[str]] = defaultdict(list)
        for _, _, meta in self.actions.values():
            groups[meta.risk].append(f"{meta.name} — {meta.description}")
        lines = []
        for risk, title in (
            ("read", "Безопасные"),
            ("write", "Изменяющие"),
            ("destructive", "Опасные"),
        ):
            if groups[risk]:
                lines.append(title + ":")
                lines.extend(f"  {item}" for item in sorted(groups[risk]))
        return "\n".join(lines)
