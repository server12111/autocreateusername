from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from database import crud


class CaptchaMiddleware(BaseMiddleware):
    """Пропускает к функционалу только тех, кто прошёл капчу. /start и ответы на капчу пропускаются всегда."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("user")
        if user is None or user.is_captcha_passed or data.get("is_admin"):
            return await handler(event, data)

        # Капча выключена в настройках — пропускаем и отмечаем пользователя как прошедшего
        session = data["session"]
        if await crud.get_setting(session, "captcha_enabled") != "1":
            user.is_captcha_passed = True
            await session.commit()
            return await handler(event, data)

        if isinstance(event, Message):
            if event.successful_payment or (event.text or "").startswith("/start"):
                return await handler(event, data)
            await event.answer("🤖 Сначала пройдите проверку на человека — отправьте /start")
            return None
        if isinstance(event, CallbackQuery):
            if (event.data or "").startswith("cap:"):
                return await handler(event, data)
            await event.answer("🤖 Сначала пройдите проверку — отправьте /start", show_alert=True)
            return None
        return await handler(event, data)
