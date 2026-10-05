from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from handlers.sections import build_ref, safe_edit

router = Router(name="referrals")


@router.callback_query(F.data == "menu:ref")
async def open_referrals(call: CallbackQuery, bot: Bot, session: AsyncSession, user: User) -> None:
    await call.answer()
    await safe_edit(call, *await build_ref(session, user, bot))
