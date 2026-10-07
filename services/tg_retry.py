"""Защита Bot API от FloodWait: если Telegram просит подождать (429 Too Many Requests),
запрос повторяется после паузы, а не теряется."""

import asyncio
import logging

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, TelegramMethod

log = logging.getLogger(__name__)

MAX_WAIT = 30  # дольше не ждём — пусть ошибка дойдёт до кода, который вызвал запрос
ATTEMPTS = 3


class RetryAfterMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request: NextRequestMiddlewareType, bot: Bot, method: TelegramMethod):
        for attempt in range(ATTEMPTS):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as e:
                # Ответ на нажатие кнопки живёт ~15 сек — ждать его бессмысленно
                if isinstance(method, AnswerCallbackQuery) or e.retry_after > MAX_WAIT or attempt == ATTEMPTS - 1:
                    raise
                log.info("Bot API: FloodWait %d сек на %s — повтор", e.retry_after, type(method).__name__)
                await asyncio.sleep(e.retry_after + 0.5)
            except TelegramBadRequest as e:
                # Нажатие обработано позже ~15 сек (перезапуск, нагрузка): ответить на него уже нельзя,
                # но сам обработчик должен отработать, а не упасть на call.answer()
                if isinstance(method, AnswerCallbackQuery) and "query is too old" in str(e):
                    return True
                raise
