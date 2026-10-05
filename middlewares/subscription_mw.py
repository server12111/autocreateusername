from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from keyboards.inline import op_kb
from services.op_manager import check_subscription
from texts import OP_TEXT


class SubscriptionMiddleware(BaseMiddleware):
    """Проверка обязательной подписки (свои каналы + Tgrass) перед ключевыми разделами."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("user")
        if user is None or data.get("is_admin"):
            return await handler(event, data)

        bot: Bot = data["bot"]
        missing = await check_subscription(bot, data["session"], user)
        if not missing:
            return await handler(event, data)

        state: FSMContext | None = data.get("state")
        if state and isinstance(event, CallbackQuery):
            await state.update_data(op_pending=event.data)

        kb = op_kb(missing)
        if isinstance(event, CallbackQuery):
            await event.answer()
            try:
                await event.message.edit_text(OP_TEXT, reply_markup=kb, disable_web_page_preview=True)
            except Exception:
                await event.message.answer(OP_TEXT, reply_markup=kb, disable_web_page_preview=True)
        elif isinstance(event, Message):
            await event.answer(OP_TEXT, reply_markup=kb, disable_web_page_preview=True)
        return None
