import tempfile
import unittest
from pathlib import Path

from userbot.core.module_installer import ModuleInstaller


class ModuleInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.installer = ModuleInstaller(root, root / "ftg", root / "native")

    def tearDown(self):
        self.temp.cleanup()

    def test_safe_module_can_be_confirmed(self):
        content = b"class Example:\n    pass\n"
        item = self.installer.stage(content, "example.py", "ftg", "test")
        installed = self.installer.confirm(item.token)
        self.assertTrue(installed.path.exists())

    def test_session_stealer_is_blocked(self):
        content = b'import glob, os\nglob.glob("*.session")\nos.system("rm -rf *")\n'
        with self.assertRaisesRegex(ValueError, "заблокирован"):
            self.installer.stage(content, "bad.py", "ftg", "test")

    def test_control_bot_token_reader_is_blocked(self):
        content = b'from pathlib import Path\nTOKEN = Path("userbot/botconfig.json").read_text()\n'
        with self.assertRaisesRegex(ValueError, "заблокирован"):
            self.installer.stage(content, "token_reader.py", "ftg", "test")


if __name__ == "__main__":
    unittest.main()
