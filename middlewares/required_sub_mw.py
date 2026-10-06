from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from keyboards.inline import required_sub_kb
from services.op_manager import check_required, notify_referrer
from texts import REQUIRED_SUB_TEXT

# Эти действия работают и без подписки: проверка подписки, капча, оплата и её проверка
_FREE_CALLBACKS = ("op:check", "cap:", "pay:chk:")


class RequiredSubscriptionMiddleware(BaseMiddleware):
    """Обязательная подписка на свои каналы администратора: без неё бот показывает только список каналов.

    Ставится после CaptchaMiddleware: сначала капча, потом подписка.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("user")
        if user is None or data.get("is_admin") or not user.is_captcha_passed:
            return await handler(event, data)
        # Оплату не блокируем. /start пропускаем, чтобы сохранить реферала, — подписку проверит show_entry
        if isinstance(event, Message) and (event.successful_payment or (event.text or "").startswith("/start")):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and (event.data or "").startswith(_FREE_CALLBACKS):
            return await handler(event, data)

        session, bot = data["session"], data["bot"]
        missing = await check_required(bot, session, user)
        if not missing:
            # Друг засчитывается пригласившему, когда прошёл капчу и подписался на обязательные каналы
            if user.referrer_id and not user.is_ref_counted:
                await notify_referrer(bot, session, user)
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer()
            if event.message:
                try:
                    await event.message.edit_text(REQUIRED_SUB_TEXT, reply_markup=required_sub_kb(missing))
                except Exception:
                    await event.message.answer(REQUIRED_SUB_TEXT, reply_markup=required_sub_kb(missing))
        elif isinstance(event, Message):
            await event.answer(REQUIRED_SUB_TEXT, reply_markup=required_sub_kb(missing))
        return None
