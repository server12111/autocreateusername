from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from config import settings
from database import crud
from database.base import session_maker


class DbSessionMiddleware(BaseMiddleware):
    """Открывает сессию БД, регистрирует/обновляет пользователя и отсекает забаненных."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with session_maker() as session:
            data["session"] = session
            tg_user = data.get("event_from_user")
            if tg_user and not tg_user.is_bot:
                user, is_new = await crud.get_or_create_user(session, tg_user)
                data["user"] = user
                data["is_new_user"] = is_new
                data["is_admin"] = tg_user.id in settings.admin_ids
                is_payment = isinstance(event, Message) and event.successful_payment is not None
                if user.is_banned and not data["is_admin"] and not is_payment:
                    if isinstance(event, CallbackQuery):
                        await event.answer("⛔️ Ваш аккаунт заблокирован администрацией.", show_alert=True)
                    elif isinstance(event, Message):
                        await event.answer("⛔️ Ваш аккаунт заблокирован администрацией.")
                    return None
            return await handler(event, data)
