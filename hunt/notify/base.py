from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from loguru import logger


@dataclass
class AppState:
    paused: bool = False
    kill_switch: bool = False

    def can_buy(self) -> bool:
        return not self.paused and not self.kill_switch


class Notifier:
    def __init__(self, bot_token: str, chat_id: int) -> None:
        self.chat_id = chat_id
        self._bot = None
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        if bot_token and chat_id:
            from aiogram import Bot
            from aiogram.client.default import DefaultBotProperties
            from aiogram.enums import ParseMode

            self._bot = Bot(bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    @property
    def enabled(self) -> bool:
        return self._bot is not None

    def start(self) -> None:
        if self.enabled:
            self._task = asyncio.get_running_loop().create_task(self._sender())

    async def stop(self) -> None:
        await self._queue.put(None)
        if self._task:
            await asyncio.wait([self._task], timeout=5)

    async def send(self, text: str) -> None:
        logger.info("NOTIFY | {}", text.replace("\n", " | "))
        if not self.enabled:
            return
        await self._queue.put(text)

    async def _sender(self) -> None:
        while True:
            text = await self._queue.get()
            if text is None:
                break
            try:
                await self._bot.send_message(self.chat_id, text[:4000])
            except Exception as e:
                logger.warning("telegram send failed: {}", e)
                await asyncio.sleep(3)
            await asyncio.sleep(0.6)


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m = seconds // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    return f"{h}h{m % 60}m"


def fmt_age(ts: int) -> str:
    return fmt_duration(max(0, time.time() - ts))
