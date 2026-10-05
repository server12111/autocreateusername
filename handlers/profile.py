from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from database import crud
from database.models import User
from handlers.sections import build_profile, safe_edit
from keyboards import inline
from texts import FAQ_TEXT

router = Router(name="profile")


class ProfileStates(StatesGroup):
    promo = State()


@router.callback_query(F.data == "menu:profile")
async def open_profile(call: CallbackQuery, bot: Bot, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.set_state(None)
    await call.answer()
    await safe_edit(call, *await build_profile(session, user, bot))


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
    text = "📁 <b>МОИ НАХОДКИ</b>\n\n"
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
