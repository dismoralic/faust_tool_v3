import tempfile
import unittest
from pathlib import Path

from userbot.core.loader import PersistentDB, _watcher_allowed, get_help_text


class LoaderStateTests(unittest.TestCase):
    def test_pointer_list_mutation_is_persisted(self):
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "db.json"
            db = PersistentDB(path)
            items = db.pointer("Example", "items", [])
            items.append("saved")
            self.assertEqual(PersistentDB(path).get("Example", "items"), ["saved"])

    def test_no_commands_tag_filters_commands(self):
        event = type(
            "Event",
            (),
            {
                "out": True,
                "is_private": True,
                "is_group": False,
                "is_channel": False,
                "chat_id": 1,
                "sender_id": 1,
                "raw_text": ".test",
            },
        )()
        self.assertFalse(_watcher_allowed(event, {"no_commands": True}))

    def test_help_is_split_into_quoted_sections(self):
        text = get_help_text()
        self.assertIn("<blockquote>", text)
        self.assertIn("Системные команды", text)
        self.assertIn("AI и Ollama", text)
        self.assertIn("FTG/Hikka-команды", text)
        self.assertIn("Панель управления", text)

    def test_dedicated_help_section(self):
        text = get_help_text("panel")
        self.assertIn(".panel status", text)
        self.assertNotIn(".фауст", text)


if __name__ == "__main__":
    unittest.main()
