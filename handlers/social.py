"""Раздел «🌐 Ник в соцсетях»: проверка ника в Telegram, YouTube, X, TikTok и поиск ника, свободного везде."""

import asyncio
import math
import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from database import crud
from database.models import User
from handlers.search import (
    _guarded,
    _premium_gate,
    _tick,
    _trap_select_text,
    _wait_message,
    format_statuses,
    network_statuses,
)
from handlers.sections import safe_edit
from keyboards import inline
from services import social_checker
from services.username_checker import CheckerUnavailable, UsernameChecker, generate_nice, normalize
from texts import SOCIAL_CHECK_PROMPT, SOCIAL_TEXT

router = Router(name="social")

CHECK_COOLDOWN = 5  # сек между проверками ника у одного пользователя (проверка бьёт в 4 сервиса)
FIND_SECONDS = 90  # сколько ищем ник, свободный везде
_last_check: dict[int, float] = {}


class SocialStates(StatesGroup):
    check = State()


def _menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔎 Проверить ник", callback_data="soc:check")
    kb.button(text="🎯 Свободный везде", callback_data="soc:find")
    kb.button(text="🪤 Ловушка в соцсетях", callback_data="s:trap")
    kb.button(text="🔙 Главное меню", callback_data="menu:main")
    kb.adjust(2, 1, 1)
    return kb.as_markup()


@router.callback_query(F.data == "menu:social")
async def social_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await call.answer()
    await safe_edit(call, SOCIAL_TEXT, _menu_kb())


# ───────────────────────── Проверка ника ─────────────────────────


@router.callback_query(F.data == "soc:check")
async def social_check_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(SocialStates.check)
    await safe_edit(call, SOCIAL_CHECK_PROMPT, inline.cancel_kb("menu:social"))


@router.message(SocialStates.check, F.text)
async def social_check_input(message: Message, user: User, state: FSMContext, checker: UsernameChecker) -> None:
    name = normalize(message.text)
    if not any(net.name_re.fullmatch(name) for code, net in social_checker.ALL.items() if code != "ig"):
        await message.answer("⚠️ Некорректный ник. Допустимы латиница, цифры, «_» и «.». Отправьте другой:",
                             reply_markup=inline.cancel_kb("menu:social"))
        return
    left = CHECK_COOLDOWN - (time.monotonic() - _last_check.get(user.tg_id, 0))
    if left > 0:
        await message.answer(f"⏱ Подождите {math.ceil(left)} сек. и отправьте ник ещё раз.")
        return
    await _social_report(message, user, state, checker, name)


@router.callback_query(F.data.startswith("soc:of:"))
async def social_of_found(call: CallbackQuery, user: User, state: FSMContext, checker: UsernameChecker) -> None:
    """Кнопка «🌐 Соцсети» под найденным ником: где ещё он свободен."""
    name = call.data.split(":", 2)[2]
    left = CHECK_COOLDOWN - (time.monotonic() - _last_check.get(user.tg_id, 0))
    if left > 0:
        await call.answer(f"⏱ Подождите {math.ceil(left)} сек.", show_alert=True)
        return
    await call.answer()
    await _social_report(call.message, user, state, checker, name)


async def _social_report(target: Message, user: User, state: FSMContext, checker: UsernameChecker, name: str) -> None:
    """Проверяет ник во всех сетях и отправляет отчёт новым сообщением."""
    _last_check[user.tg_id] = time.monotonic()
    wait = await target.answer(f"⏳ Проверяю @{name} в Telegram и соцсетях…")
    statuses = await network_statuses(checker, name)
    catchable = [code for code, (st, _) in statuses.items() if st in ("taken", "unknown")]
    text = f"🌐 <b>@{name}</b>\n\n{format_statuses(name, statuses)}"
    free = [social_checker.ALL[c].title for c, (st, _) in statuses.items() if st == "free"]
    if free:
        text += f"\n\n⚡️ Свободен в: <b>{', '.join(free)}</b> — занимайте, пока не забрали."

    # Данные для ловушки: если пользователь захочет ловить ник там, где он занят
    await state.set_state(None)
    await state.update_data(trap_name=name, trap_catchable=catchable,
                            trap_selected=["tg"] if "tg" in catchable else catchable[:1],
                            trap_text=format_statuses(name, statuses))
    kb = InlineKeyboardBuilder()
    if catchable:
        kb.button(text="🪤 Ловить, где занят", callback_data="soc:trap")
    kb.button(text="🔎 Проверить другой", callback_data="soc:check")
    kb.button(text="🔙 Назад", callback_data="menu:social")
    kb.adjust(1)
    await wait.edit_text(text, reply_markup=kb.as_markup(), disable_web_page_preview=True)


@router.callback_query(F.data == "soc:trap")
async def social_to_trap(call: CallbackQuery, user: User, state: FSMContext) -> None:
    if not await _premium_gate(call, user):
        return
    data = await state.get_data()
    if not data.get("trap_name") or not data.get("trap_catchable"):
        await call.answer("Проверьте ник заново", show_alert=True)
        return
    await call.answer()
    await safe_edit(call, _trap_select_text(data["trap_name"], data["trap_text"]),
                    inline.trap_networks_kb(data["trap_catchable"], data["trap_selected"]))


# ───────────────────────── Ник, свободный везде ─────────────────────────


@router.callback_query(F.data == "soc:find")
async def social_find(call: CallbackQuery, session: AsyncSession, user: User, checker: UsernameChecker) -> None:
    if not await _premium_gate(call, user):
        return
    await _guarded(call, user, _find_everywhere(call, session, user, checker))


async def _find_everywhere(call: CallbackQuery, session: AsyncSession, user: User, checker: UsernameChecker) -> None:
    wait_text = ("⏳ <b>Ищу ник, свободный везде</b>\n\n"
                 "Проверяю варианты в X, YouTube, TikTok и Telegram…")
    msg = await _wait_message(call, wait_text)
    ticker = asyncio.create_task(_tick(msg, wait_text))
    found = None
    try:
        seen = await crud.recently_checked_by_user(session, user.tg_id, since_hours=24 * 7)
        deadline = time.monotonic() + FIND_SECONDS
        while not found and time.monotonic() < deadline:
            batch = []
            while len(batch) < 12:
                n = generate_nice(6)
                if n not in seen:
                    seen.add(n)
                    batch.append(n)
            # Сначала дешёвые проверки соцсетей, Telegram (лимиты аккаунтов) — только для прошедших
            for name in await social_checker.free_everywhere(batch):
                res = await checker.check(name)
                if res.is_free:
                    found = name
                    break
    except CheckerUnavailable:
        found = None
    finally:
        ticker.cancel()

    kb = InlineKeyboardBuilder()
    if not found:
        kb.button(text="🔄 Попробовать ещё", callback_data="soc:find")
        kb.button(text="🔙 Назад", callback_data="menu:social")
        kb.adjust(1)
        await msg.edit_text("😔 За полторы минуты не нашлось ника, свободного сразу везде. Попробуйте ещё раз.",
                            reply_markup=kb.as_markup())
        return

    row = await crud.add_search(session, user.tg_id, found, True, True, "free")
    user.total_searches_done += 1
    await session.commit()
    statuses = {"tg": ("free", social_checker.STATUS_MARK["free"])}
    statuses.update({c: ("free", social_checker.STATUS_MARK["free"]) for c in social_checker.SOCIAL})
    kb.button(text="🔄 Найти ещё", callback_data="soc:find")
    kb.button(text="📁 В мои находки", callback_data=f"s:save:{row.id}")
    kb.button(text="🔙 Назад", callback_data="menu:social")
    kb.adjust(2, 1)
    await msg.edit_text(
        f"🎉 <b>@{found}</b> свободен везде!\n\n{format_statuses(found, statuses)}\n\n"
        "⚡️ Занимайте скорее — сразу во всех сетях, пока не забрали.",
        reply_markup=kb.as_markup(),
        disable_web_page_preview=True,
    )
