from __future__ import annotations

import json
import os
import re
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BOT_TOKEN_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")
BOT_USERNAME_RE = re.compile(r"^fausttool_[a-z0-9]{6}_bot$")
BOT_COMMANDS = """start - первичная настройка
panel - панель управления
help - инструкция
status - состояние юзербота
health - проверка Ollama
ai - статистика AI
modules - список модулей
actions - список AI-действий
autoreply - правила автоответчика
files - сохранённые файлы"""


def load_bot_config(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Некорректный botconfig.json: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("botconfig.json должен содержать JSON-объект")
    return value


def save_bot_config(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def generate_bot_username() -> str:
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"fausttool_{suffix}_bot"


def extract_bot_token(text: str) -> str | None:
    match = BOT_TOKEN_RE.search(str(text or ""))
    return match.group(0) if match else None


async def _exchange(conversation: Any, text: str):
    await conversation.send_message(text)
    return await conversation.get_response()


async def _configure_setting(conversation: Any, command: str, username: str, value: str) -> bool:
    try:
        await _exchange(conversation, command)
        await _exchange(conversation, f"@{username}")
        response = await _exchange(conversation, value)
        response_text = str(getattr(response, "raw_text", "") or "").lower()
        return not any(marker in response_text for marker in ("error", "invalid", "ошиб", "невер"))
    except Exception:
        try:
            await _exchange(conversation, "/cancel")
        except Exception:
            pass
        return False


async def register_control_bot(user_client: Any, path: Path) -> dict:
    async with user_client.conversation("@BotFather", timeout=90, exclusive=True) as conversation:
        try:
            await _exchange(conversation, "/cancel")
        except Exception:
            pass
        await _exchange(conversation, "/newbot")
        await _exchange(conversation, "faust-tool")

        token = None
        username = None
        last_response = ""
        for _ in range(10):
            candidate = generate_bot_username()
            response = await _exchange(conversation, candidate)
            last_response = str(getattr(response, "raw_text", "") or "")
            token = extract_bot_token(last_response)
            if token:
                username = candidate
                break
            lowered = last_response.lower()
            if any(marker in lowered for marker in ("too many bots", "слишком много ботов")):
                raise RuntimeError("BotFather не разрешает создать ещё одного бота для этого аккаунта")
        if not token or not username:
            raise RuntimeError("BotFather не выдал распознаваемый токен нового бота")

        config = {
            "token": token,
            "username": username,
            "name": "faust-tool",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "inline_enabled": False,
            "setup_complete": False,
        }
        save_bot_config(path, config)

        config["inline_enabled"] = await _configure_setting(
            conversation,
            "/setinline",
            username,
            "Панель управления Faust Tool",
        )
        config["commands_configured"] = await _configure_setting(
            conversation,
            "/setcommands",
            username,
            BOT_COMMANDS,
        )
        save_bot_config(path, config)
        return config


async def ensure_control_bot(user_client: Any, path: Path) -> tuple[dict, bool]:
    config = load_bot_config(path)
    token = str(config.get("token") or "").strip()
    username = str(config.get("username") or "").strip().lstrip("@")
    if token and username:
        config["username"] = username
        return config, False
    return await register_control_bot(user_client, path), True


def mark_setup_complete(path: Path, config: dict) -> None:
    updated = dict(config)
    updated["setup_complete"] = True
    updated["setup_completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_bot_config(path, updated)
