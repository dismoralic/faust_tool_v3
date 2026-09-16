import asyncio
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from userbot.ai.autoreply import AutoreplyContextStore
from userbot.ai.autoreply_worker import AutoreplyWorker
from userbot.ai.brain import AIBrain


class FakeClient:
    async def iter_messages(self, chat_id, limit):
        for item in []:
            yield item

    @asynccontextmanager
    async def action(self, *args):
        yield


class AutoreplyWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name)
        self.brain = AIBrain("http://127.0.0.1:11434", "qwen3:1.7b", str(path / "ai"), owner_id=10)
        self.brain.generate_messages = AsyncMock(return_value="нормально вроде, сам как?")
        self.store = AutoreplyContextStore(path / "contexts", 10)
        self.worker = AutoreplyWorker(FakeClient(), self.brain, self.store, cooldown=0)
        self.brain.select_autoreply("persona", "all")
        self.sender = SimpleNamespace(id=20, contact=False)

    async def asyncTearDown(self):
        await self.brain.close()
        self.temp.cleanup()

    def message(self, chat_id=20, message_id=1):
        sent = SimpleNamespace(id=message_id + 1000, chat_id=chat_id, sender_id=10, out=True, raw_text="нормально вроде, сам как?")
        return SimpleNamespace(
            id=message_id, chat_id=chat_id, sender_id=20, out=False, raw_text="как дела",
            reply=AsyncMock(return_value=sent),
        )

    async def take_job(self, message):
        self.assertTrue(self.worker.enqueue(message, self.sender, is_private=message.chat_id > 0))
        return await self.worker.queue.get()

    async def test_disabled_before_processing_uses_no_generation(self):
        message = self.message()
        job = await self.take_job(message)
        self.brain.select_autoreply(None, "chat", 20)
        await self.worker.process(job)
        self.brain.generate_messages.assert_not_called()
        message.reply.assert_not_called()

    async def test_disabled_during_generation_does_not_send(self):
        message = self.message()
        job = await self.take_job(message)

        async def generate(*args, **kwargs):
            self.brain.select_autoreply(None, "all")
            return "у меня все хорошо"

        self.brain.generate_messages.side_effect = generate
        await self.worker.process(job)
        message.reply.assert_not_called()

    async def test_off_then_on_does_not_revive_old_queue(self):
        message = self.message()
        job = await self.take_job(message)
        self.brain.select_autoreply(None, "chat", 20)
        self.brain.select_autoreply("persona", "chat", 20)
        await self.worker.process(job)
        self.brain.generate_messages.assert_not_called()

    async def test_other_chat_disable_does_not_cancel_this_chat(self):
        message = self.message()
        job = await self.take_job(message)
        self.brain.select_autoreply(None, "chat", -100123)
        await self.worker.process(job)
        message.reply.assert_awaited_once()
        data = self.store._load(20)
        self.assertTrue(data["messages"][-1]["generated_by_ai"])
        self.assertFalse(data["messages"][-1]["style_eligible"])

    async def test_disable_during_cooldown_stops_send(self):
        message = self.message()
        job = await self.take_job(message)
        self.worker.cooldown = 5
        self.worker.last_sent_at = time.monotonic()

        async def wait(delay):
            self.assertGreater(delay, 0)
            self.brain.select_autoreply(None, "chat", 20)

        with patch("userbot.ai.autoreply_worker.asyncio.sleep", side_effect=wait):
            await self.worker.process(job)
        message.reply.assert_not_called()

    async def test_echo_retried_once_then_good_reply_sent(self):
        message = self.message()
        job = await self.take_job(message)
        self.brain.generate_messages.side_effect = ["как дела", "нормально вроде, сам как?"]
        await self.worker.process(job)
        self.assertEqual(self.brain.generate_messages.await_count, 2)
        message.reply.assert_awaited_once_with("нормально вроде, сам как?", parse_mode=None)

    async def test_two_echoes_do_not_send_repetitive_placeholder(self):
        message = self.message()
        job = await self.take_job(message)
        self.brain.generate_messages.side_effect = ["как дела", "Привет, как дела."]
        await self.worker.process(job)
        self.assertEqual(self.brain.generate_messages.await_count, 2)
        message.reply.assert_not_called()

    async def test_queue_deduplicates_and_cleans_completed_jobs(self):
        message = self.message()
        self.assertTrue(self.worker.enqueue(message, self.sender, is_private=True))
        self.assertFalse(self.worker.enqueue(message, self.sender, is_private=True))
        task = asyncio.create_task(self.worker.run())
        try:
            await asyncio.wait_for(self.worker.queue.join(), timeout=2)
            self.assertFalse(self.worker.queued_ids)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cooldown_is_shared_between_two_chats(self):
        first, second = self.message(20, 1), self.message(-100123, 2)
        await self.worker.process(await self.take_job(first))
        self.worker.cooldown = 5
        with patch("userbot.ai.autoreply_worker.asyncio.sleep", new_callable=AsyncMock) as wait:
            await self.worker.process(await self.take_job(second))
            wait.assert_awaited_once()
            self.assertGreater(wait.call_args.args[0], 0)
            self.assertLessEqual(wait.call_args.args[0], 5)
        second.reply.assert_awaited_once()
