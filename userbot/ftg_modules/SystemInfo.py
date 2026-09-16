import os
import time

from .. import loader, utils


@loader.tds
class SystemInfoMod(loader.Module):
    strings = {"name": "SystemInfo"}

    def __init__(self):
        self.started_at = time.monotonic()

    @loader.command()
    async def ftgpingcmd(self, message):
        """Проверить работу FTG-совместимого загрузчика"""
        started = time.monotonic()
        edited = await utils.answer(message, "Проверка...")
        elapsed = (time.monotonic() - started) * 1000
        await utils.answer(edited, f"FTG loader работает. {elapsed:.1f} мс")

    @loader.command()
    async def sysinfocmd(self, message):
        """Показать базовую информацию о процессе юзербота"""
        uptime = int(time.monotonic() - self.started_at)
        try:
            load = ", ".join(f"{value:.2f}" for value in os.getloadavg())
        except (AttributeError, OSError):
            load = "недоступно"
        await utils.answer(
            message,
            f"PID: {os.getpid()}\nUptime: {uptime} сек.\nLoad average: {load}",
        )
