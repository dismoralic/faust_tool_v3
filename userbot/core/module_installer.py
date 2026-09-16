from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class PendingModule:
    token: str
    kind: str
    filename: str
    path: Path
    sha256: str
    source: str
    warnings: list[str]
    expires_at: float


class ModuleInstaller:
    MAX_SIZE = 512 * 1024
    CRITICAL_PATTERNS = {
        r"rm\s+-rf": "обнаружено рекурсивное удаление файлов",
        r"shutil\.rmtree": "обнаружено рекурсивное удаление каталогов",
        r"glob\s*\([^\n]*\.session": "обнаружен поиск Telegram-сессий",
        r"(?:open|Path)\s*\([^\n]*\.session": "обнаружено чтение Telegram-сессии",
        r"botconfig\.json": "обнаружено обращение к токену личного inline-бота",
        r"pkill\s+python": "обнаружено завершение Python-процессов",
        r"authorized_keys": "обнаружена работа с SSH authorized_keys",
    }
    WARNING_PATTERNS = {
        r"\bos\.system\s*\(": "используется os.system",
        r"\bsubprocess\.": "используется subprocess",
        r"\beval\s*\(": "используется eval",
        r"\bexec\s*\(": "используется exec",
        r"\bsocket\.": "используются прямые сетевые сокеты",
        r"\brequests\.": "модуль делает внешние HTTP-запросы",
    }

    def __init__(self, root: Path, ftg_dir: Path, native_dir: Path):
        self.root = root
        self.ftg_dir = ftg_dir
        self.native_dir = native_dir
        self.pending_dir = root / "pending_modules"
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.pending: dict[str, PendingModule] = {}

    @staticmethod
    def _safe_filename(filename: str) -> str:
        name = Path(filename or "module.py").name
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        if not name.endswith(".py"):
            name += ".py"
        if name.startswith(".") or name in {"__init__.py", "loader.py", "utils.py"}:
            raise ValueError("Недопустимое имя модуля")
        return name

    @classmethod
    def scan(cls, source: str) -> tuple[list[str], list[str]]:
        critical = [message for pattern, message in cls.CRITICAL_PATTERNS.items() if re.search(pattern, source, re.I)]
        warnings = [message for pattern, message in cls.WARNING_PATTERNS.items() if re.search(pattern, source, re.I)]
        return sorted(set(critical)), sorted(set(warnings))

    def stage(self, content: bytes, filename: str, kind: str, source: str) -> PendingModule:
        if kind not in {"ftg", "native"}:
            raise ValueError("Неизвестный тип модуля")
        if not content or len(content) > self.MAX_SIZE:
            raise ValueError(f"Размер модуля должен быть от 1 до {self.MAX_SIZE // 1024} КБ")
        filename = self._safe_filename(filename)
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Модуль должен быть в UTF-8") from exc
        compile(text, filename, "exec")
        critical, warnings = self.scan(text)
        if critical:
            raise ValueError("Модуль заблокирован: " + "; ".join(critical))

        token = secrets.token_hex(3).upper()
        path = self.pending_dir / f"{token}_{filename}"
        path.write_bytes(content)
        item = PendingModule(
            token=token,
            kind=kind,
            filename=filename,
            path=path,
            sha256=hashlib.sha256(content).hexdigest(),
            source=source,
            warnings=warnings,
            expires_at=time.monotonic() + 300,
        )
        self.pending[token] = item
        return item

    def describe(self, item: PendingModule) -> str:
        lines = [
            f"Модуль подготовлен: {item.filename}",
            f"Тип: {item.kind}",
            f"SHA-256: {item.sha256}",
        ]
        if item.warnings:
            lines.append("Предупреждения: " + "; ".join(item.warnings))
        else:
            lines.append("Опасные шаблоны не найдены, но проверка не гарантирует безопасность.")
        lines.append(f"Установка в течение 5 минут: .modconfirm {item.token}")
        lines.append("Отмена: .modcancel")
        return "\n".join(lines)

    def confirm(self, token: str) -> PendingModule:
        token = token.strip().upper()
        item = self.pending.pop(token, None)
        if not item:
            raise ValueError("Код установки не найден")
        if time.monotonic() > item.expires_at:
            item.path.unlink(missing_ok=True)
            raise ValueError("Время подтверждения истекло")
        target_dir = self.ftg_dir if item.kind == "ftg" else self.native_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / item.filename
        os.replace(item.path, target)
        item.path = target
        return item

    def cancel_all(self) -> int:
        items = list(self.pending.values())
        self.pending.clear()
        for item in items:
            item.path.unlink(missing_ok=True)
        return len(items)
