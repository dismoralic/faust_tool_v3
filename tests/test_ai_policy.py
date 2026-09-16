import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from userbot.ai.brain import AIBrain, OllamaError, ai_function


class DummyBlock:
    async def execute(self, *args, **kwargs):
        return "выполнено"


class AIPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "blocks").mkdir()
        self.brain = AIBrain("http://127.0.0.1:11434", "qwen3:1.7b", str(root), owner_id=10)

        @ai_function(
            description="Удаляет тест",
            triggers=["удали тест"],
            risk="destructive",
            owner_only=True,
            name="delete_test",
        )
        async def delete_test(client=None, message=None):
            return "выполнено"

        @ai_function(
            description="Показывает тестовый чат",
            triggers=["покажи чат"],
            params=[{"name": "target", "type": "string"}],
            risk="read",
            name="chat_test",
        )
        async def chat_test(target=None, client=None, message=None):
            return str(target)

        @ai_function(
            description="Сохраняет файл из реплая",
            triggers=["сохрани файл"],
            params=[{"name": "alias", "type": "string", "required": True}],
            risk="write",
            name="download_test",
        )
        async def download_test(alias: str, client=None, message=None):
            return alias

        self.brain.actions = {
            "delete_test": (DummyBlock(), delete_test, delete_test._ai_meta),
            "chat_test": (DummyBlock(), chat_test, chat_test._ai_meta),
            "download_test": (DummyBlock(), download_test, download_test._ai_meta),
        }

    async def asyncTearDown(self):
        await self.brain.close()
        self.temp.cleanup()

    async def test_every_command_is_understood_by_neural_router(self):
        calls = 0

        async def chat(messages, **kwargs):
            nonlocal calls
            calls += 1
            return '{"steps":[],"answer":"Хорошо, удалять не буду.","uncertain":false,"clarification":"","suggestions":[]}'

        self.brain.ollama.chat = chat
        result = await self.brain.process_message(10, "owner", "не трогай это пожалуйста", True, chat_id=1)
        self.assertEqual(result, "Хорошо, удалять не буду.")
        self.assertEqual(calls, 1)

    async def test_destructive_action_requires_confirmation(self):
        async def chat(messages, **kwargs):
            return '{"action":"delete_test","params":{},"answer":""}'

        self.brain.ollama.chat = chat
        result = await self.brain.process_message(
            10, "owner", "удали тест", True, client=object(), message=object(), chat_id=1
        )
        self.assertIn("Требуется подтверждение", result)
        token = next(iter(self.brain.pending))
        confirmed = await self.brain.confirm_action(10, token, client=object(), message=object())
        self.assertEqual(confirmed, "выполнено")

    async def test_tool_catalog_uses_closed_json_schema(self):
        catalog = self.brain._tool_catalog()
        self.assertEqual(catalog[0]["type"], "function")
        parameters = catalog[0]["function"]["parameters"]
        self.assertEqual(parameters["type"], "object")
        self.assertFalse(parameters["additionalProperties"])

    async def test_neural_router_is_converted_to_candidate(self):
        async def chat(messages, **kwargs):
            return '{"action":"delete_test","params":{},"answer":""}'

        self.brain.ollama.chat = chat
        candidate = await self.brain._ai_candidate("удали тест")
        self.assertEqual(candidate.name, "delete_test")
        self.assertEqual(candidate.source, "neural_router")

    async def test_router_sees_full_action_catalog(self):
        seen = []

        async def chat(messages, **kwargs):
            schema = kwargs["output_schema"]
            seen.extend(schema["properties"]["steps"]["items"]["properties"]["action"]["enum"])
            return '{"steps":[],"answer":"обычный ответ","uncertain":false,"clarification":"","suggestions":[]}'

        self.brain.ollama.chat = chat
        await self.brain._ai_candidate("скажи что-нибудь непредсказуемое")
        self.assertEqual(set(seen), set(self.brain.actions))

    async def test_alias_is_extracted_from_natural_replied_file_request(self):
        async def chat(messages, **kwargs):
            return '{"steps":[{"action":"download_test","params":{}}],"answer":"","uncertain":false,"clarification":"","suggestions":[]}'

        self.brain.ollama.chat = chat
        candidate = await self.brain._ai_candidate("сохрани его с названием дискорд")
        self.assertEqual(candidate.name, "download_test")
        self.assertEqual(candidate.params["alias"], "дискорд")

    async def test_router_receives_replied_file_context(self):
        captured = ""

        class RepliedCommand:
            chat_id = 123
            is_private = True
            is_group = False
            is_reply = True

            async def get_reply_message(self):
                return SimpleNamespace(
                    id=7,
                    sender_id=20,
                    media=object(),
                    file=SimpleNamespace(name="discord.zip", mime_type="application/zip"),
                    raw_text="",
                )

        async def chat(messages, **kwargs):
            nonlocal captured
            captured = messages[-1]["content"]
            return '{"steps":[{"action":"download_test","params":{}}],"answer":"","uncertain":false,"clarification":"","suggestions":[]}'

        self.brain.ollama.chat = chat
        await self.brain._ai_candidate("сохрани его с названием дискорд", message=RepliedCommand())
        self.assertIn("reply=True", captured)
        self.assertIn("reply_has_media=True", captured)
        self.assertIn("discord.zip", captured)

    async def test_uncertain_router_asks_and_suggests(self):
        async def chat(messages, **kwargs):
            return '{"steps":[],"answer":"","uncertain":true,"clarification":"Файл сохранить или отправить?","suggestions":[".фауст сохрани его с названием документ",".фауст скинь файл документ Артему"]}'

        self.brain.ollama.chat = chat
        result = await self.brain.process_message(10, "owner", "сделай что-нибудь с этим", True, chat_id=1)
        self.assertIn("Я не уверен", result)
        self.assertIn("Файл сохранить или отправить?", result)
        self.assertIn(".фауст сохрани его", result)

    async def test_json_fallback_is_used_when_schema_is_unavailable(self):
        calls = 0
        async def chat(messages, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OllamaError("Ollama HTTP 400: schema unsupported")
            return '{"action":"delete_test","params":{},"answer":""}'

        self.brain.ollama.chat = chat
        candidate = await self.brain._ai_candidate("удали тест")
        self.assertEqual(candidate.name, "delete_test")
        self.assertEqual(candidate.source, "neural_router")
        self.assertEqual(calls, 2)

    async def test_router_cannot_invent_target(self):
        async def chat(messages, **kwargs):
            return '{"action":"chat_test","params":{"target":"703136036"},"answer":""}'

        self.brain.ollama.chat = chat
        candidate = await self.brain._ai_candidate("дай информацию об этом чате")
        self.assertNotIn("target", candidate.params)

    async def test_router_returns_normal_answer_without_second_request(self):
        calls = 0

        async def chat(messages, **kwargs):
            nonlocal calls
            calls += 1
            return '{"action":"none","params":{},"answer":"Я уже ответил."}'

        self.brain.ollama.chat = chat
        result = await self.brain.process_message(
            10, "owner", "покажи пример хорошего ответа", True, chat_id=1
        )
        self.assertEqual(result, "Я уже ответил.")
        self.assertEqual(calls, 1)

    async def test_multi_step_plan_uses_one_confirmation(self):
        async def chat(messages, **kwargs):
            return '{"steps":[{"action":"chat_test","params":{}},{"action":"delete_test","params":{}}],"answer":""}'

        self.brain.ollama.chat = chat
        result = await self.brain.process_message(
            10, "owner", "покажи чат и удали тест", True, client=object(), message=object(), chat_id=1
        )
        self.assertIn("план из 2 шагов", result)
        self.assertEqual(len(self.brain.pending), 1)
        token = next(iter(self.brain.pending))
        confirmed = await self.brain.confirm_action(10, token, client=object(), message=object())
        self.assertIn("План выполнен", confirmed)
        self.assertEqual(confirmed.count("выполнено"), 2)

    async def test_autoreply_scopes_and_priority(self):
        self.brain.configure_autoreply("assistant", "chat", True, -100123)
        self.assertEqual(self.brain.autoreply_mode(99, -100123, False), "assistant")
        self.brain.configure_autoreply("persona", "user", True, 99)
        self.assertEqual(self.brain.autoreply_mode(99, -100123, False), "persona")
        self.brain.configure_autoreply("persona", "user", False, 99)
        self.assertEqual(self.brain.autoreply_mode(99, -100123, False), "assistant")

    async def test_chat_off_beats_global_on(self):
        self.brain.select_autoreply("persona", "all")
        self.brain.select_autoreply(None, "chat", -100123)
        self.assertIsNone(self.brain.autoreply_mode(99, -100123, False))
        self.assertEqual(self.brain.autoreply_mode(99, -100999, False), "persona")
        self.brain.select_autoreply("assistant", "chat", -100123)
        self.assertEqual(self.brain.autoreply_mode(99, -100123, False), "assistant")
        self.assertEqual(self.brain.autoreply_mode(99, -100999, False), "persona")

    async def test_all_off_disables_individual_and_legacy_rules(self):
        self.brain.configure_autoreply("persona", "user", True, 99)
        self.brain.configure_autoreply("assistant", "chat", True, -100123)
        self.brain.set_answer(None, True)
        self.brain.select_autoreply(None, "all")
        self.assertIsNone(self.brain.resolve_autoreply_mode(99, 99, False, is_private=True))
        self.assertIsNone(self.brain.resolve_autoreply_mode(20, -100123, False, is_private=False))

    async def test_specific_chat_can_override_general_contact_rule(self):
        self.brain.configure_autoreply("persona", "contacts", False)
        self.brain.select_autoreply("persona", "chat", 99)
        self.assertEqual(self.brain.autoreply_mode(99, 99, True), "persona")

    async def test_old_answer_cannot_bypass_disabled_chat(self):
        self.brain.set_answer(None, True)
        self.brain.select_autoreply(None, "chat", 99)
        self.assertIsNone(self.brain.resolve_autoreply_mode(99, 99, False, is_private=True))
        self.assertEqual(self.brain.resolve_autoreply_mode(20, 20, False, is_private=True), "assistant")

    async def test_autoreply_exceptions_survive_settings_reload(self):
        self.brain.select_autoreply("persona", "all")
        self.brain.select_autoreply(None, "chat", -100123)
        self.brain.settings = self.brain._load_settings()
        self.assertIsNone(self.brain.autoreply_mode(99, -100123, False))
        self.assertEqual(self.brain.autoreply_mode(99, -100999, False), "persona")

    async def test_tickets_invalidate_only_affected_scope(self):
        ticket = self.brain.autoreply_ticket(99, -100123, False)
        other = self.brain.autoreply_ticket(99, -100999, False)
        self.brain.select_autoreply(None, "chat", -100123)
        self.assertNotEqual(ticket, self.brain.autoreply_ticket(99, -100123, False))
        self.assertEqual(other, self.brain.autoreply_ticket(99, -100999, False))



if __name__ == "__main__":
    unittest.main()
