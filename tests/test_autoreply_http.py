import tempfile
import unittest
from pathlib import Path

from aiohttp import web

from userbot.ai.autoreply import AutoreplyContextStore, generate_autoreply_response
from userbot.ai.brain import AIBrain


class AutoreplyHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.requests = []
        self.responses = []

        async def chat(request):
            self.requests.append(await request.json())
            return web.json_response({
                "model": "qwen3:1.7b",
                "message": {"role": "assistant", "content": self.responses.pop(0)},
                "done": True,
            })

        app = web.Application()
        app.router.add_post("/api/chat", chat)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        self.brain = AIBrain(f"http://127.0.0.1:{port}", "qwen3:1.7b", str(Path(self.temp.name) / "ai"))
        self.store = AutoreplyContextStore(Path(self.temp.name) / "context", 10)
        self.data = {
            "chat_id": 20, "schema_version": 2,
            "messages": [
                {"id": 1, "sender_id": 20, "text": "привет", "out": False},
                {"id": 2, "sender_id": 10, "text": "здарова", "out": True, "style_eligible": True},
            ],
        }

    async def asyncTearDown(self):
        await self.brain.close()
        await self.runner.cleanup()
        self.temp.cleanup()

    async def test_real_http_sends_native_roles_and_disables_thinking(self):
        self.responses = ["<think>internal</think>все ок, сам как?"]
        result = await generate_autoreply_response(self.brain, self.store, self.data, "как дела", "persona", sender_id=20, incoming_id=3)
        self.assertEqual(result, "все ок, сам как?")
        self.assertEqual(len(self.requests), 1)
        body = self.requests[0]
        self.assertFalse(body["think"])
        self.assertFalse(body["stream"])
        self.assertEqual([item["role"] for item in body["messages"]], ["system", "user", "assistant", "user"])
        self.assertEqual(body["options"]["num_ctx"], 3072)

    async def test_bad_reply_retries_over_http_once(self):
        self.responses = ["как дела", "у меня все спокойно, как у тебя?"]
        result = await generate_autoreply_response(self.brain, self.store, self.data, "как дела", "persona")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result, "у меня все спокойно, как у тебя?")
        self.assertIn("черновик отклонён", self.requests[1]["messages"][0]["content"])

    async def test_assistant_mode_is_labelled(self):
        self.responses = ["Могу помочь разобраться."]
        result = await generate_autoreply_response(self.brain, self.store, self.data, "помоги", "assistant")
        self.assertEqual(result, "AI-ассистент: Могу помочь разобраться.")
