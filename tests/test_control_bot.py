import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from userbot.inline.control_bot import ControlBotUnavailable, FaustControlBot


class FakeCallback:
    def __init__(self, sender_id, data=b"faust:status"):
        self.sender_id = sender_id
        self.data = data
        self.answers = []
        self.edits = []

    async def answer(self, text="", alert=False):
        self.answers.append((text, alert))

    async def edit(self, text, **kwargs):
        self.edits.append((text, kwargs))


class BrokenInlineClient:
    async def inline_query(self, *args, **kwargs):
        raise RuntimeError("inline forbidden")


class FakeMessageEvent:
    chat_id = 1
    is_reply = False


class ControlBotTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, path):
        return FaustControlBot(
            api_id=1,
            api_hash="hash",
            token="123456789:" + "A" * 35,
            username="fausttool_a1b2c3_bot",
            owner_id=10,
            session_path=path,
            prefix=".",
            action_handler=lambda action: action,
        )

    async def test_foreign_callback_is_rejected(self):
        with tempfile.TemporaryDirectory() as value:
            bot = self.make_bot(Path(value) / "bot")
            event = FakeCallback(999)
            await bot._on_callback(event)
        self.assertEqual(event.answers, [("Нет доступа.", True)])

    async def test_inline_error_switches_to_fallback_exception(self):
        with tempfile.TemporaryDirectory() as value:
            bot = self.make_bot(Path(value) / "bot")
            with self.assertRaises(ControlBotUnavailable):
                await bot.open_inline(BrokenInlineClient(), FakeMessageEvent())

    async def test_current_chat_autoreply_action_uses_panel_context(self):
        calls = []

        async def handler(action, chat_id):
            calls.append((action, chat_id))
            return "готово"

        with tempfile.TemporaryDirectory() as value:
            bot = FaustControlBot(
                api_id=1,
                api_hash="hash",
                token="123456789:" + "A" * 35,
                username="fausttool_a1b2c3_bot",
                owner_id=10,
                session_path=Path(value) / "bot",
                prefix=".",
                action_handler=handler,
            )
            token = bot._remember_context(-1001234567890)
            event = FakeCallback(10, f"faust:{token}:ar_persona_on_chat".encode())
            await bot._on_callback(event)

        self.assertEqual(calls, [("ar_persona_on_chat", -1001234567890), ("ar_status_chat", -1001234567890)])
        self.assertTrue(event.edits)
        self.assertIn("готово", event.edits[0][0])

    async def test_current_chat_button_without_context_is_rejected(self):
        with tempfile.TemporaryDirectory() as value:
            bot = self.make_bot(Path(value) / "bot")
            event = FakeCallback(10, b"faust:menu_autoreply_chat")
            await bot._on_callback(event)
        self.assertEqual(event.answers, [("Открой панель командой .panel в нужном чате.", True)])

    async def test_expired_token_cannot_toggle_current_chat(self):
        with tempfile.TemporaryDirectory() as value:
            bot = self.make_bot(Path(value) / "bot")
            handler = AsyncMock()
            bot.action_handler = handler
            event = FakeCallback(10, b"faust:expired:ar_disable_chat")
            await bot._on_callback(event)
            handler.assert_not_called()
            self.assertTrue(event.answers[0][1])

    async def test_inline_query_carries_actual_origin_and_delete_failure_keeps_panel(self):
        with tempfile.TemporaryDirectory() as value:
            bot = self.make_bot(Path(value) / "bot")
            result = SimpleNamespace(click=AsyncMock())
            client = SimpleNamespace(inline_query=AsyncMock(return_value=[result]))
            event = SimpleNamespace(
                chat_id=-100777, is_reply=False,
                get_chat=AsyncMock(return_value=SimpleNamespace(title="Тестовая группа")),
                delete=AsyncMock(side_effect=RuntimeError("delete forbidden")),
            )
            await bot.open_inline(client, event)
            token = client.inline_query.call_args.args[1].split()[1]
            self.assertEqual(bot._context_chat(token), -100777)
            self.assertIn("Тестовая группа", bot._home_text(token))
            result.click.assert_awaited_once_with(-100777, reply_to=None)

    async def test_buttons_fit_telegram_callback_limit_and_have_single_off(self):
        with tempfile.TemporaryDirectory() as value:
            bot = self.make_bot(Path(value) / "bot")
            token = bot._remember_context(-100111)
            data = [button.data for row in bot._autoreply_scope_buttons("chat", token) for button in row]
            self.assertTrue(all(len(item) <= 64 for item in data))
            self.assertEqual(sum(b"ar_disable_chat" in item for item in data), 1)

    async def test_malformed_action_not_dispatched(self):
        with tempfile.TemporaryDirectory() as value:
            bot = self.make_bot(Path(value) / "bot")
            handler = AsyncMock()
            bot.action_handler = handler
            event = FakeCallback(10, b"faust:ar_unexpected_action")
            await bot._on_callback(event)
            handler.assert_not_called()

    async def test_inline_result_buttons_keep_the_origin_token(self):
        with tempfile.TemporaryDirectory() as value:
            bot = self.make_bot(Path(value) / "bot")
            token = bot._remember_context(-100555, "Группа")
            event = SimpleNamespace(
                sender_id=10, text=f"panel {token}",
                builder=SimpleNamespace(article=AsyncMock(return_value="article")),
                answer=AsyncMock(),
            )
            await bot._on_inline(event)
            kwargs = event.builder.article.call_args.kwargs
            payloads = [button.data for row in kwargs["buttons"] for button in row]
            self.assertTrue(all(item.startswith(f"faust:{token}:".encode()) for item in payloads))
            self.assertIn("-100555", kwargs["text"])
            event.answer.assert_awaited_once_with(["article"], cache_time=0, private=True)


if __name__ == "__main__":
    unittest.main()
