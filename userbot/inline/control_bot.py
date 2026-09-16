from __future__ import annotations

import html
import inspect
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger
from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError


ActionHandler = Callable[..., Awaitable[str] | str]


class ControlBotUnavailable(RuntimeError):
    pass


class FaustControlBot:
    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        token: str,
        username: str,
        owner_id: int,
        session_path: Path,
        prefix: str,
        action_handler: ActionHandler,
    ):
        self.api_id = int(api_id)
        self.api_hash = str(api_hash)
        self.token = str(token)
        self.username = str(username).lstrip("@")
        self.owner_id = int(owner_id)
        self.session_path = Path(session_path)
        self.prefix = prefix
        self.action_handler = action_handler
        self.client: TelegramClient | None = None
        self._panel_contexts: dict[str, tuple[int, float]] = {}
        self._panel_titles: dict[str, str] = {}

    def _home_text(self, token: str | None = None) -> str:
        context = self._context_caption(token)
        return (
            "<b>Faust Tool v3</b>\n"
            "<blockquote>Личная панель управления юзерботом. "
            f"Все действия выполняются тем же процессом Faust Tool.</blockquote>\n{context}"
        )

    def _context_caption(self, token: str | None) -> str:
        chat_id = self._context_chat(token)
        if chat_id is None:
            return f"<i>Для управления конкретным чатом вызови <code>{html.escape(self.prefix)}panel</code> в нём.</i>"
        title = html.escape(self._panel_titles.get(token, "Чат вызова панели"))
        return f"<b>{title}</b> · <code>{chat_id}</code>"

    @staticmethod
    def _payload(action: str, token: str | None = None) -> bytes:
        value = f"faust:{token}:{action}" if token else f"faust:{action}"
        return value.encode("utf-8")

    def _remember_context(self, chat_id: int, title: str = "") -> str:
        now = time.monotonic()
        self._panel_contexts = {
            token: value
            for token, value in self._panel_contexts.items()
            if now - value[1] < 21600
        }
        self._panel_titles = {key: value for key, value in self._panel_titles.items() if key in self._panel_contexts}
        token = secrets.token_hex(4)
        self._panel_contexts[token] = (int(chat_id), now)
        if title:
            self._panel_titles[token] = title[:180]
        return token

    def _context_chat(self, token: str | None) -> int | None:
        if not token:
            return None
        value = self._panel_contexts.get(token)
        if not value or time.monotonic() - value[1] >= 21600:
            self._panel_contexts.pop(token, None)
            return None
        return int(value[0])

    def _buttons(self, token: str | None = None):
        return [
            [
                Button.inline("AI", self._payload("menu_ai", token)),
                Button.inline("Модули", self._payload("menu_modules", token)),
            ],
            [
                Button.inline("Автоответ", self._payload("menu_autoreply", token)),
                Button.inline("Файлы", self._payload("menu_files", token)),
            ],
            [
                Button.inline("Система", self._payload("menu_system", token)),
                Button.inline("Закрыть", self._payload("close", token)),
            ],
        ]

    def _section_buttons(self, section: str, token: str | None = None):
        menus = {
            "ai": [
                [
                    Button.inline("Статистика", self._payload("ai", token)),
                    Button.inline("Действия", self._payload("actions", token)),
                ],
                [
                    Button.inline("Проверить Ollama", self._payload("health", token)),
                    Button.inline("Обновить блоки", self._payload("reload", token)),
                ],
            ],
            "modules": [[Button.inline("Список модулей", self._payload("modules", token))]],
            "autoreply": [
                [Button.inline("Текущие правила", self._payload("autoreply", token))],
                [
                    Button.inline("Для всех", self._payload("menu_autoreply_all", token)),
                    Button.inline("Этот чат", self._payload("menu_autoreply_chat", token)),
                ],
            ],
            "files": [[Button.inline("Список файлов", self._payload("files", token))]],
            "system": [
                [
                    Button.inline("Состояние", self._payload("status", token)),
                    Button.inline("Инструкция", self._payload("help", token)),
                ],
            ],
        }
        return [
            *menus[section],
            [
                Button.inline("Назад", self._payload("home", token)),
                Button.inline("Закрыть", self._payload("close", token)),
            ],
        ]

    def _autoreply_scope_buttons(self, scope: str, token: str | None = None):
        return [
            [
                Button.inline("ВКЛ: от моего имени", self._payload(f"ar_persona_on_{scope}", token)),
            ],
            [
                Button.inline("ВКЛ: AI-ассистент", self._payload(f"ar_assistant_on_{scope}", token)),
            ],
            [
                Button.inline("Выключить автоответ", self._payload(f"ar_disable_{scope}", token)),
            ],
            [
                Button.inline("Назад", self._payload("menu_autoreply", token)),
                Button.inline("Закрыть", self._payload("close", token)),
            ],
        ]

    def _section_text(self, section: str, context_chat_id: int | None = None) -> str:
        titles = {
            "ai": ("AI", "Модель, маршрутизация и цепочки Telegram-действий."),
            "modules": ("Модули", "FTG/Hikka-совместимые и native-модули."),
            "autoreply": ("Автоответчик", "Режим владельца и отдельный AI-ассистент."),
            "files": ("Файлы", "Локальные файлы, сохранённые под понятными ярлыками."),
            "system": ("Система", "Состояние Faust Tool и инструкция по управлению."),
        }
        title, description = titles[section]
        if section == "autoreply":
            context = (
                f"Текущий чат панели: <code>{context_chat_id}</code>."
                if context_chat_id is not None
                else f"Текущий чат панели не определён — открой её командой <code>{html.escape(self.prefix)}panel</code> в нужном чате."
            )
            description = f"{description}\n{context}"
        return f"<b>{title}</b>\n<blockquote>{description}</blockquote>"

    def _back_buttons(self, token: str | None = None):
        return [[
            Button.inline("Назад", self._payload("home", token)),
            Button.inline("Закрыть", self._payload("close", token)),
        ]]

    def _instruction(self) -> str:
        p = html.escape(self.prefix)
        return (
            "<b>Первичная настройка завершена</b>\n"
            "<blockquote>Этот бот принадлежит только текущему владельцу Faust Tool. "
            "Он показывает inline-панель, а действия выполняет Telethon-юзербот.</blockquote>\n\n"
            "<b>Использование</b>\n"
            f"<blockquote><code>{p}panel</code> — открыть панель в текущем чате\n"
            f"<code>{p}panel status</code> — командный режим\n"
            f"<code>{p}panel retry</code> — повторить inline-вызов\n"
            f"<code>{p}panelhelp</code> — полная справка панели\n\n"
            "В личном чате с этим ботом доступны /panel, /status, /health, /ai, "
            "/modules, /actions, /autoreply и /files.</blockquote>\n\n"
            "<blockquote>Если inline запрещён в конкретном чате, Faust Tool покажет "
            "команды управления вместо панели.</blockquote>"
        )

    @staticmethod
    def _render_result(title: str, value: str) -> str:
        clean = str(value or "Пустой результат")
        if len(clean) > 3200:
            clean = clean[:3197] + "..."
        return f"<b>{html.escape(title)}</b>\n<blockquote>{html.escape(clean)}</blockquote>"

    async def _run_action(self, action: str, context_chat_id: int | None = None) -> str:
        accepts_context = False
        try:
            parameters = inspect.signature(self.action_handler).parameters.values()
            accepts_context = any(item.kind == item.VAR_POSITIONAL for item in parameters) or sum(
                item.kind in {item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD}
                for item in parameters
            ) >= 2
        except (TypeError, ValueError):
            pass
        result = (
            self.action_handler(action, context_chat_id)
            if accepts_context
            else self.action_handler(action)
        )
        if inspect.isawaitable(result):
            result = await result
        return str(result)

    async def start(self) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(
            str(self.session_path),
            self.api_id,
            self.api_hash,
            device_model="Faust Control Bot",
            system_version="Ubuntu 24.04",
            app_version="3.5.1",
        )
        self.client.add_event_handler(self._on_start, events.NewMessage(pattern=r"^/(?:start|help)(?:@\w+)?$"))
        self.client.add_event_handler(self._on_panel, events.NewMessage(pattern=r"^/panel(?:@\w+)?$"))
        self.client.add_event_handler(
            self._on_command,
            events.NewMessage(pattern=r"^/(status|health|ai|modules|actions|autoreply|files)(?:@\w+)?$"),
        )
        self.client.add_event_handler(self._on_inline, events.InlineQuery())
        self.client.add_event_handler(self._on_callback, events.CallbackQuery(pattern=b"^faust:"))
        await self.client.start(bot_token=self.token)
        me = await self.client.get_me()
        if getattr(me, "username", None):
            self.username = me.username
        logger.info(f"Inline-панель запущена: @{self.username}")

    async def stop(self) -> None:
        if self.client and self.client.is_connected():
            await self.client.disconnect()

    async def send_first_start(self, user_client: Any) -> None:
        await user_client.send_message(f"@{self.username}", "/start", parse_mode=None)

    async def open_inline(self, user_client: Any, event: Any) -> None:
        title = ""
        try:
            chat = await event.get_chat()
            title = getattr(chat, "title", "") or " ".join(filter(None, (
                getattr(chat, "first_name", ""), getattr(chat, "last_name", ""),
            ))) or getattr(chat, "username", "")
        except Exception:
            pass
        token = self._remember_context(event.chat_id, title)
        try:
            results = await user_client.inline_query(
                f"@{self.username}",
                f"panel {token}",
                entity=event.chat_id,
            )
            if not results:
                raise ControlBotUnavailable("inline-бот не вернул панель")
            reply_to = event.reply_to_msg_id if getattr(event, "is_reply", False) else None
            await results[0].click(event.chat_id, reply_to=reply_to)
        except ControlBotUnavailable:
            self._panel_contexts.pop(token, None)
            raise
        except Exception as exc:
            self._panel_contexts.pop(token, None)
            raise ControlBotUnavailable(str(exc)) from exc
        try:
            await event.delete()
        except Exception:
            logger.debug("Панель отправлена, но исходную команду удалить не удалось")

    async def _deny_message(self, event: Any) -> bool:
        if int(event.sender_id or 0) == self.owner_id:
            return False
        await event.respond("Нет доступа.", parse_mode=None)
        return True

    async def _on_start(self, event: Any) -> None:
        if await self._deny_message(event):
            return
        await event.respond(self._instruction(), buttons=self._buttons(), parse_mode="html")

    async def _on_panel(self, event: Any) -> None:
        if await self._deny_message(event):
            return
        await event.respond(self._home_text(), buttons=self._buttons(), parse_mode="html")

    async def _on_command(self, event: Any) -> None:
        if await self._deny_message(event):
            return
        action = str(event.pattern_match.group(1) or "status")
        value = await self._run_action(action)
        await event.respond(self._render_result(action.upper(), value), buttons=self._back_buttons(), parse_mode="html")

    async def _on_inline(self, event: Any) -> None:
        if int(event.sender_id or 0) != self.owner_id:
            await event.answer([], cache_time=0, private=True)
            return
        query = str(
            getattr(event, "text", "")
            or getattr(getattr(event, "query", None), "query", "")
            or ""
        ).strip()
        token = query.split(maxsplit=1)[1].strip() if query.startswith("panel ") else None
        if self._context_chat(token) is None:
            token = None
        result = await event.builder.article(
            title="Faust Tool — панель",
            description="Личная панель управления юзерботом",
            text=self._home_text(token),
            buttons=self._buttons(token),
            parse_mode="html",
        )
        await event.answer([result], cache_time=0, private=True)

    @staticmethod
    async def _edit(event: Any, text: str, **kwargs) -> None:
        try:
            await event.edit(text, **kwargs)
        except MessageNotModifiedError:
            pass

    async def _show_autoreply_scope(self, event: Any, scope: str, token: str | None, notice: str = "") -> None:
        chat_id = self._context_chat(token)
        status = await self._run_action(f"ar_status_{scope}", chat_id)
        title = "Автоответ: для всех" if scope == "all" else "Автоответ: этот чат"
        location = self._context_caption(token) if scope == "chat" else ""
        text = f"<b>{title}</b>\n{location}\n"
        if notice:
            text += f"<blockquote>{html.escape(notice)}</blockquote>\n"
        text += f"<blockquote>{html.escape(status[:2400])}</blockquote>\n"
        text += "<i>Выбор режима заменяет другой режим в этой области. Выключение чата важнее общего включения.</i>"
        await self._edit(event, text, buttons=self._autoreply_scope_buttons(scope, token), parse_mode="html")

    async def _on_callback(self, event: Any) -> None:
        if int(event.sender_id or 0) != self.owner_id:
            await event.answer("Нет доступа.", alert=True)
            return
        parts = bytes(event.data).decode("utf-8", "ignore").split(":")
        if len(parts) >= 3:
            token, action = parts[1], ":".join(parts[2:])
        else:
            token, action = None, parts[-1]
        context_chat_id = self._context_chat(token)
        if action == "close":
            await event.answer()
            try:
                await event.delete()
            except Exception:
                await event.edit("<b>Панель закрыта</b>", buttons=None, parse_mode="html")
            return
        if action == "home":
            await event.answer()
            await self._edit(event, self._home_text(token), buttons=self._buttons(token), parse_mode="html")
            return
        if action == "help":
            await event.answer()
            await self._edit(event, self._instruction(), buttons=self._back_buttons(token), parse_mode="html")
            return
        if action in {"menu_autoreply_all", "menu_autoreply_chat"}:
            scope = action.rsplit("_", 1)[-1]
            if scope == "chat" and context_chat_id is None:
                await event.answer(
                    f"Открой панель командой {self.prefix}panel в нужном чате.",
                    alert=True,
                )
                return
            await event.answer()
            await self._show_autoreply_scope(event, scope, token)
            return
        if action.startswith("menu_"):
            section = action.split("_", 1)[-1]
            if section not in {"ai", "modules", "autoreply", "files", "system"}:
                await event.answer("Неизвестный раздел.", alert=True)
                return
            await event.answer()
            await self._edit(event,
                self._section_text(section, context_chat_id),
                buttons=self._section_buttons(section, token),
                parse_mode="html",
            )
            return
        allowed = {"status", "health", "ai", "modules", "actions", "reload", "autoreply", "files"}
        toggles = {f"ar_{mode}_{state}_{scope}" for mode in ("persona", "assistant") for state in ("on", "off") for scope in ("all", "chat")}
        toggles.update({"ar_disable_all", "ar_disable_chat"})
        if action not in allowed | toggles:
            await event.answer("Неизвестное действие.", alert=True)
            return
        if action in toggles and action.endswith("_chat") and context_chat_id is None:
            await event.answer(f"Панель устарела. Вызови {self.prefix}panel заново в нужном чате.", alert=True)
            return
        await event.answer("Выполняю...")
        try:
            value = await self._run_action(action, context_chat_id)
        except Exception as exc:
            value = f"Ошибка: {exc}"
        if action in toggles:
            await self._show_autoreply_scope(event, action.rsplit("_", 1)[-1], token, value)
            return
        await self._edit(event,
            self._render_result("АВТООТВЕТ" if action.startswith("ar_") else action.upper(), value),
            buttons=(
                self._section_buttons("autoreply", token)
                if action.startswith("ar_")
                else self._back_buttons(token)
            ),
            parse_mode="html",
        )
