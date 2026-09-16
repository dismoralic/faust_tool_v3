from __future__ import annotations

import getpass
import json
import os
import platform
import re
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "userbot" / "config.json"
EXAMPLE_PATH = ROOT / "userbot" / "config.example.json"


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def deep_merge(default: dict, loaded: dict) -> dict:
    result = dict(default)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def ask(label: str, default: str = "", *, hidden: bool = False) -> str:
    suffix = " [оставить текущее]" if hidden and default else (f" [{default}]" if default else "")
    reader = getpass.getpass if hidden else input
    value = reader(f"{label}{suffix}: ").strip()
    return value or default


def valid_api_id(value: object) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def valid_api_hash(value: object) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{32}", str(value or "").strip()))


def valid_url(value: object) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def detect_system() -> str:
    if "com.termux" in os.environ.get("PREFIX", ""):
        return "Android Termux"
    return platform.system() or "Unknown"


def main() -> int:
    defaults = load_json(EXAMPLE_PATH)
    if not defaults:
        print(f"Не найден или повреждён шаблон {EXAMPLE_PATH}")
        return 1
    current = load_json(CONFIG_PATH)
    config = deep_merge(defaults, current)
    configured = valid_api_id(config.get("API_ID")) and valid_api_hash(config.get("API_HASH"))
    if configured:
        answer = ask("Конфигурация уже заполнена. Изменить её? y/N", "N").lower()
        if answer not in {"y", "yes", "д", "да"}:
            print("Текущая конфигурация сохранена без изменений.")
            return 0
    while True:
        api_id = ask("Telegram API_ID", str(config.get("API_ID") or ""))
        if valid_api_id(api_id):
            config["API_ID"] = int(api_id)
            break
        print("API_ID должен быть положительным числом.")
    while True:
        api_hash = ask("Telegram API_HASH", str(config.get("API_HASH") or ""), hidden=True)
        if valid_api_hash(api_hash):
            config["API_HASH"] = api_hash.lower()
            break
        print("API_HASH должен содержать 32 шестнадцатеричных символа.")
    while True:
        ollama_url = ask("Адрес Ollama", str(config.get("ollama_url") or defaults["ollama_url"]))
        if valid_url(ollama_url):
            config["ollama_url"] = ollama_url.rstrip("/")
            break
        print("Адрес должен начинаться с http:// или https:// и содержать имя хоста.")
    config["model"] = ask("Модель Ollama", str(config.get("model") or defaults["model"]))
    config["system_version"] = detect_system()
    version_path = ROOT / "VERSION"
    if version_path.exists():
        config["app_version"] = version_path.read_text(encoding="utf-8").strip()
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    print(f"Конфигурация сохранена: {CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
