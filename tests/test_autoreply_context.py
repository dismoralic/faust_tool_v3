import tempfile
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from userbot.ai.autoreply import AutoreplyContextStore


class FakeClient:
    def __init__(self):
        self.calls = 0

    async def iter_messages(self, chat_id, limit):
        self.calls += 1
        for index in range(3, 0, -1):
            yield SimpleNamespace(
                id=index,
                sender_id=10 if index == 2 else 20,
                out=index == 2,
                date=datetime.now(timezone.utc),
                raw_text="у меня норм" if index == 2 else f"message {index}",
            )


class AutoreplyContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_history_is_not_refetched_same_day(self):
        with tempfile.TemporaryDirectory() as value:
            store = AutoreplyContextStore(Path(value), 10, refresh_seconds=86400, limit=100)
            client = FakeClient()
            first = await store.ensure(client, 123)
            second = await store.ensure(client, 123)
            self.assertEqual(client.calls, 1)
            self.assertEqual(len(first["messages"]), 3)
            self.assertEqual(first, second)
            system, prompt = store.prompt(second, "new", "persona")
            self.assertIn("ВЛАДЕЛЕЦ", prompt)
            self.assertIn("Сначала отвечай по смыслу", system)

    async def test_generated_reply_is_not_used_as_owner_style(self):
        with tempfile.TemporaryDirectory() as value:
            store = AutoreplyContextStore(Path(value), 10)
            client = FakeClient()
            await store.ensure(client, 123)
            generated = SimpleNamespace(
                id=44,
                chat_id=123,
                sender_id=10,
                out=True,
                date=datetime.now(timezone.utc),
                raw_text="Привет, как я и говорил.",
            )
            store.append(generated, generated_by_ai=True)
            store.append(generated)
            data = store._load(123)
            system, prompt = store.prompt(data, "как дела", "persona")
            self.assertIn("НЕУДАЧНЫЙ_АВТООТВЕТ: Привет, как я и говорил.", prompt)
            self.assertNotIn("ВЛАДЕЛЕЦ: Привет, как я и говорил.", prompt)
            self.assertIn(44, data["generated_ids"])
            self.assertIn("никогда не считай стилем владельца", system)
            native = store.chat_messages(data, "как дела", "persona", sender_id=20)
            self.assertNotIn("Привет, как я и говорил.", str(native))

    def test_persona_quality_rejects_echoes(self):
        samples = (
            ("а что ты говорил", "Привет, что я и говорил."),
            ("как дела", "Привет, как дела."),
            ("а че ты как даун общаешься", "а че ты как даун общаешься"),
        )
        for incoming, response in samples:
            with self.subTest(response=response):
                self.assertIsNotNone(
                    AutoreplyContextStore.persona_quality_issue(incoming, response)
                )
        self.assertIsNone(
            AutoreplyContextStore.persona_quality_issue("как дела", "нормально вроде, сам как?")
        )

    def test_greeting_can_be_returned_as_greeting(self):
        self.assertIsNone(AutoreplyContextStore.persona_quality_issue("привет!", "Привет"))
        self.assertIsNone(AutoreplyContextStore.persona_quality_issue("доброе утро", "доброе утро!"))

    def test_repeated_autoreply_is_detected(self):
        self.assertIsNotNone(AutoreplyContextStore.persona_quality_issue(
            "какие планы на вечер", "вроде все нормально а у тебя", ("вроде все нормально а у тебя",),
        ))

    async def test_native_roles_current_message_once_and_future_messages_excluded(self):
        with tempfile.TemporaryDirectory() as value:
            store = AutoreplyContextStore(Path(value), 10)
            data = await store.ensure(FakeClient(), 123)
            native = store.chat_messages(data, "message 3", "persona", sender_id=20, incoming_id=3)
            self.assertEqual(native[-1], {"role": "user", "content": "Собеседник 20: message 3"})
            self.assertEqual(str(native).count("message 3"), 1)
            self.assertTrue(any(item["role"] == "assistant" for item in native))
            early = store.chat_messages(data, "message 1", "persona", incoming_id=1)
            self.assertNotIn("message 3", str(early))

    async def test_generated_mark_survives_refresh_and_outgoing_event(self):
        with tempfile.TemporaryDirectory() as value:
            store = AutoreplyContextStore(Path(value), 10)
            await store.ensure(FakeClient(), 123)
            message = SimpleNamespace(id=2, chat_id=123, sender_id=10, out=True, raw_text="у меня норм")
            store.append(message, generated_by_ai=True)
            store.append(message)
            data = store._load(123)
            data["refreshed_at"] = 1
            store._save(123, data)
            data = await store.ensure(FakeClient(), 123)
            item = next(item for item in data["messages"] if item["id"] == 2)
            self.assertTrue(item["generated_by_ai"])
            self.assertFalse(item["style_eligible"])

    async def test_legacy_context_is_not_blindly_trusted_as_owner_style(self):
        with tempfile.TemporaryDirectory() as value:
            store = AutoreplyContextStore(Path(value), 10)
            store._path(123).write_text(json.dumps({
                "chat_id": 123, "refreshed_at": 1,
                "messages": [{"id": 2, "sender_id": 10, "out": True, "text": "старый ответ"}],
            }), encoding="utf-8")
            data = store._load(123)
            self.assertFalse(data["messages"][0]["style_eligible"])
            self.assertNotIn("старый ответ", store.chat_messages(data, "привет", "persona")[0]["content"])

    async def test_appending_old_queued_message_does_not_reorder_history(self):
        with tempfile.TemporaryDirectory() as value:
            store = AutoreplyContextStore(Path(value), 10)
            await store.ensure(FakeClient(), 123)
            store.append(SimpleNamespace(id=1, chat_id=123, sender_id=20, out=False, raw_text="message 1"))
            self.assertEqual([item["id"] for item in store._load(123)["messages"]], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
