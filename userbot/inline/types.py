from __future__ import annotations


class InlineCall:
    """Адаптер над Telethon CallbackQuery для импортов и базовых callback-модулей."""

    def __init__(self, event=None):
        self.event = event

    def __getattr__(self, name):
        if self.event is None:
            raise AttributeError(name)
        return getattr(self.event, name)

    async def answer(self, text=None, show_alert=False, **kwargs):
        if self.event is None:
            return False
        return await self.event.answer(text or "", alert=show_alert, **kwargs)

    async def edit(self, text, **kwargs):
        if self.event is None:
            return False
        return await self.event.edit(text, **kwargs)

    async def delete(self):
        if self.event is None:
            return False
        return await self.event.delete()


class BotInlineCall(InlineCall):
    pass


class BotInlineMessage(InlineCall):
    pass


class InlineQuery(InlineCall):
    pass
