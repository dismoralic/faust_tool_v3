from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any


class FileLibrary:
    def __init__(self, files_dir: Path, index_file: Path):
        self.files_dir = Path(files_dir).resolve()
        self.index_file = Path(index_file)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        self.items = self._load()

    @staticmethod
    def normalize_alias(alias: str) -> str:
        value = re.sub(r"\s+", " ", str(alias or "").strip().lower())
        value = re.sub(r"[^0-9a-zа-яё _.-]", "", value)
        if not value or len(value) > 80:
            raise ValueError("Ярлык должен содержать от 1 до 80 обычных символов")
        return value

    @staticmethod
    def _safe_name(name: str) -> str:
        value = Path(str(name or "file.bin")).name
        value = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._ -]", "_", value).strip(" .")
        return value[:150] or "file.bin"

    def _load(self) -> dict:
        try:
            value = json.loads(self.index_file.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        temp = self.index_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.items, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.index_file)

    def _inside(self, path: Path) -> Path:
        resolved = path.resolve()
        if resolved != self.files_dir and self.files_dir not in resolved.parents:
            raise ValueError("Путь файла вышел за пределы хранилища юзербота")
        return resolved

    async def download_replied(self, client: Any, message: Any, alias: str) -> dict:
        if message is None or not getattr(message, "is_reply", False):
            raise ValueError("Ответь командой на сообщение с файлом, фото, видео или другим медиа")
        reply = await message.get_reply_message()
        if reply is None or not (getattr(reply, "media", None) or getattr(reply, "file", None)):
            raise ValueError("В сообщении из реплая нет скачиваемого файла")
        key = self.normalize_alias(alias)
        if key in self.items:
            raise ValueError(f"Ярлык «{key}» уже занят; сначала удали старую запись")
        suggested = getattr(getattr(reply, "file", None), "name", None) or f"telegram_{reply.id}.bin"
        name = f"{int(time.time())}_{self._safe_name(suggested)}"
        destination = self._inside(self.files_dir / name)
        downloaded = await client.download_media(reply, file=str(destination))
        if not downloaded:
            raise ValueError("Telegram не вернул скачанный файл")
        path = self._inside(Path(downloaded))
        item = {
            "alias": key,
            "path": str(path.relative_to(self.files_dir)),
            "filename": path.name,
            "source_chat": getattr(reply, "chat_id", None),
            "source_message": getattr(reply, "id", None),
            "saved_at": int(time.time()),
        }
        self.items[key] = item
        self._save()
        return dict(item)

    def resolve(self, alias: str) -> tuple[Path, dict]:
        key = self.normalize_alias(alias)
        item = self.items.get(key)
        if not isinstance(item, dict):
            raise ValueError(f"Файл с ярлыком «{key}» не найден")
        path = self._inside(self.files_dir / str(item.get("path") or ""))
        if not path.is_file():
            raise ValueError(f"Файл «{key}» записан в каталоге, но отсутствует на диске")
        return path, dict(item)

    def delete(self, alias: str) -> str:
        key = self.normalize_alias(alias)
        path, item = self.resolve(key)
        path.unlink()
        self.items.pop(key, None)
        self._save()
        return str(item.get("filename") or path.name)

    def list_text(self) -> str:
        if not self.items:
            return "Сохранённых файлов пока нет. Ответь на медиа: .фауст сохрани файл с ярлыком документы"
        lines = ["Сохранённые файлы:"]
        for key, item in sorted(self.items.items()):
            exists = (self.files_dir / str(item.get("path") or "")).is_file()
            lines.append(f"• {key} — {item.get('filename', 'файл')} ({'готов' if exists else 'потерян'})")
        return "\n".join(lines)
