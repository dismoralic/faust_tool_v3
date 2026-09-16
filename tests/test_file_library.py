import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from userbot.core.file_library import FileLibrary


class FakeClient:
    async def download_media(self, reply, file):
        Path(file).write_bytes(b"content")
        return file


class FakeMessage:
    is_reply = True

    def __init__(self):
        self.reply = SimpleNamespace(
            id=7,
            chat_id=123,
            media=object(),
            file=SimpleNamespace(name="Документ.pdf"),
        )

    async def get_reply_message(self):
        return self.reply


class FileLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_resolve_and_delete(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            library = FileLibrary(root / "files", root / "index.json")
            item = await library.download_replied(FakeClient(), FakeMessage(), "Договор")
            self.assertEqual(item["alias"], "договор")
            path, _ = library.resolve("ДОГОВОР")
            self.assertEqual(path.read_bytes(), b"content")
            self.assertIn("договор", library.list_text())
            library.delete("договор")
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
