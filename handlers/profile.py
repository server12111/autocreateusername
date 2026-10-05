import random
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from database import crud
from database.models import User, utcnow
from handlers.sections import build_profile, fmt_timedelta, safe_edit
from keyboards import inline
from texts import FAQ_TEXT, LINE

router = Router(name="profile")


class ProfileStates(StatesGroup):
    promo = State()


@router.callback_query(F.data == "menu:profile")
async def open_profile(call: CallbackQuery, bot: Bot, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.set_state(None)
    await call.answer()
    await safe_edit(call, *await build_profile(session, user, bot))


@router.callback_query(F.data == "prof:bonus")
async def daily_bonus(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    now = utcnow()
    if user.last_bonus_claim and now - user.last_bonus_claim < timedelta(hours=24):
        left = timedelta(hours=24) - (now - user.last_bonus_claim)
        await call.answer(f"⏳ Следующий бонус через {fmt_timedelta(left.total_seconds())}", show_alert=True)
        return
    amount = random.randint(1, 3)
    user.free_searches_left += amount
    user.last_bonus_claim = now
    await session.commit()
    await call.answer(f"🎁 Ежедневный бонус получен: +{amount} поиск(а)!", show_alert=True)


@router.callback_query(F.data == "prof:promo")
async def promo_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(ProfileStates.promo)
    await safe_edit(call, "🎟 <b>Введите промокод</b>\n\nОтправьте код сообщением:", inline.cancel_kb("menu:profile"))


@router.message(ProfileStates.promo, F.text)
async def promo_input(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    ok, text = await crud.activate_promocode(session, user, message.text)
    if ok:
        await state.set_state(None)
        await message.answer(text, reply_markup=inline.back_kb("menu:profile", "👤 В профиль"))
    else:
        await message.answer(text + "\n\nПопробуйте другой код:", reply_markup=inline.cancel_kb("menu:profile"))


@router.callback_query(F.data == "prof:finds")
async def my_findings(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    rows = await crud.get_findings(session, user.tg_id)
    text = f"📁 <b>МОИ НАХОДКИ</b>\n{LINE}\n\n"
    if rows:
        for r in rows:
            star = "⭐️ " if r.is_saved else ""
            text += f"{star}<b>@{r.username_query}</b> — {r.created_at:%d.%m.%Y %H:%M}\n"
        text += "\n<i>⭐️ — сохранённые вами. Ник мог быть занят после проверки.</i>"
    else:
        text += "Здесь появятся свободные юзернеймы, найденные вами в поиске."
    await call.answer()
    await safe_edit(call, text, inline.back_kb("menu:profile", "🔙 В профиль"))


@router.callback_query(F.data == "prof:info")
async def info(call: CallbackQuery) -> None:
    await call.answer()
    await safe_edit(call, FAQ_TEXT, inline.back_kb("menu:profile", "🔙 В профиль"))
