from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from userbot.ai.autoreply import AutoreplyContextStore, generate_autoreply_response


@dataclass
class ReplyJob:
    message: Any
    sender: Any
    text: str
    mode: str
    ticket: tuple[int, ...]
    is_private: bool


class AutoreplyWorker:
    def __init__(
        self, client: Any, brain: Any, store: AutoreplyContextStore, *,
        cooldown: float = 5.0, queue_size: int = 1000, legacy_private_only: bool = True,
    ):
        self.client = client
        self.brain = brain
        self.store = store
        self.cooldown = max(0.0, float(cooldown))
        self.legacy_private_only = legacy_private_only
        self.queue: asyncio.Queue[ReplyJob] = asyncio.Queue(maxsize=max(1, int(queue_size)))
        self.queued_ids: set[tuple[int, int]] = set()
        self.last_sent_at = 0.0

    def _mode(self, message: Any, sender: Any, is_private: bool) -> str | None:
        return self.brain.resolve_autoreply_mode(
            sender.id, message.chat_id, bool(getattr(sender, "contact", False)),
            is_private=is_private, legacy_private_only=self.legacy_private_only,
        )

    def _ticket(self, message: Any, sender: Any) -> tuple[int, ...]:
        return self.brain.autoreply_ticket(sender.id, message.chat_id, bool(getattr(sender, "contact", False)))

    def enqueue(self, message: Any, sender: Any, *, is_private: bool) -> bool:
        text = str(getattr(message, "raw_text", "") or "").strip()
        mode = self._mode(message, sender, is_private)
        key = (int(message.chat_id), int(message.id))
        if not text or mode is None or key in self.queued_ids:
            return False
        job = ReplyJob(message, sender, text[-6000:], mode, self._ticket(message, sender), is_private)
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            logger.warning("Очередь автоответчика заполнена; сообщение пропущено")
            return False
        self.queued_ids.add(key)
        return True

    def _active(self, job: ReplyJob) -> bool:
        return (
            self._ticket(job.message, job.sender) == job.ticket
            and self._mode(job.message, job.sender, job.is_private) == job.mode
        )

    async def process(self, job: ReplyJob) -> None:
        if not self._active(job):
            return
        data = await self.store.ensure(self.client, job.message.chat_id)
        data = self.store.append(job.message) or data
        if not self._active(job):
            return
        async with self.client.action(job.message.chat_id, "typing"):
            response = await generate_autoreply_response(
                self.brain, self.store, data, job.text, job.mode,
                sender_id=int(job.sender.id), incoming_id=int(job.message.id),
                active=lambda: self._active(job),
            )
        if not response or not self._active(job):
            return
        delay = self.cooldown - (time.monotonic() - self.last_sent_at)
        if delay > 0:
            await asyncio.sleep(delay)
        if not self._active(job):
            return
        sent = await job.message.reply(response[:3900], parse_mode=None)
        self.last_sent_at = time.monotonic()
        self.store.append(sent, generated_by_ai=True)

    async def run(self) -> None:
        while True:
            job = await self.queue.get()
            key = (int(job.message.chat_id), int(job.message.id))
            try:
                await self.process(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"Ошибка автоответчика: chat={job.message.chat_id}, mode={job.mode}")
            finally:
                self.queued_ids.discard(key)
                self.queue.task_done()
