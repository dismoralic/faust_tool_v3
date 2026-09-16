import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from userbot.core.bot_registration import (
    BOT_USERNAME_RE,
    extract_bot_token,
    generate_bot_username,
    load_bot_config,
    register_control_bot,
)


class FakeResponse:
    def __init__(self, text):
        self.raw_text = text


class FakeConversation:
    def __init__(self, token):
        self.token = token
        self.sent = []
        self.last = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send_message(self, text):
        self.last = text
        self.sent.append(text)

    async def get_response(self):
        if BOT_USERNAME_RE.fullmatch(self.last):
            return FakeResponse(f"Done. Use this token: {self.token}")
        return FakeResponse("OK")


class FakeClient:
    def __init__(self, conversation):
        self.value = conversation

    def conversation(self, *args, **kwargs):
        return self.value


class BotRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def test_empty_config_means_not_registered(self):
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "botconfig.json"
            path.write_text("", encoding="utf-8")
            self.assertEqual(load_bot_config(path), {})

    def test_generated_username_matches_required_format(self):
        self.assertRegex(generate_bot_username(), BOT_USERNAME_RE)

    def test_token_extraction(self):
        token = "123456789:" + "A" * 35
        self.assertEqual(extract_bot_token(f"Token: {token}"), token)

    async def test_registration_saves_bot_and_enables_inline(self):
        token = "123456789:" + "B" * 35
        conversation = FakeConversation(token)
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "botconfig.json"
            with patch(
                "userbot.core.bot_registration.generate_bot_username",
                return_value="fausttool_a1b2c3_bot",
            ):
                result = await register_control_bot(FakeClient(conversation), path)
            stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["token"], token)
        self.assertEqual(stored["username"], "fausttool_a1b2c3_bot")
        self.assertTrue(stored["inline_enabled"])
        self.assertIn("/setinline", conversation.sent)
        self.assertIn("/setcommands", conversation.sent)


if __name__ == "__main__":
    unittest.main()
