from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from database import crud
from database.models import User
from handlers.sections import BATTLE_POOL, build_battle, safe_edit
from keyboards import inline

router = Router(name="battle")


async def _enabled(call: CallbackQuery, session: AsyncSession) -> bool:
    if await crud.get_setting(session, "battle_enabled") == "1":
        return True
    await call.answer("⚔️ Битва никнеймов временно отключена", show_alert=True)
    return False


@router.callback_query(F.data == "menu:battle")
async def open_battle(call: CallbackQuery, bot: Bot, session: AsyncSession, user: User) -> None:
    if not await _enabled(call, session):
        return
    await call.answer()
    await safe_edit(call, *await build_battle(session, user, bot))


@router.callback_query(F.data.startswith("bt:v:"))
async def vote(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    _, _, wi, li = call.data.split(":")
    try:
        winner, loser = BATTLE_POOL[int(wi)], BATTLE_POOL[int(li)]
    except (ValueError, IndexError):
        await call.answer("Битва устарела", show_alert=True)
        return
    if not await crud.add_battle_vote(session, user.tg_id, winner, loser):
        await call.answer("Вы уже голосовали в этой паре — держите новую битву!")
        await safe_edit(call, *await build_battle(session, user, call.bot))
        return
    w, l = await crud.battle_pair_stats(session, winner, loser)
    total = w + l or 1
    wp = round(w / total * 100)
    text = (
        f"⚔️ <b>РЕЗУЛЬТАТ БИТВЫ</b>\n\n"
        f"Ваш голос: <b>@{winner}</b> 👑\n\n"
        f"🔴 @{winner} — <b>{wp}%</b> ({w})\n"
        f"🔵 @{loser} — <b>{100 - wp}%</b> ({l})\n\n"
        f"Всего голосов в этой паре: {w + l}"
    )
    await call.answer("✅ Голос учтён!")
    await safe_edit(call, text, inline.battle_next_kb())


@router.callback_query(F.data == "bt:top")
async def top(call: CallbackQuery, session: AsyncSession) -> None:
    rows = await crud.battle_top(session)
    text = "🏆 <b>ТОП-10 НИКНЕЙМОВ СООБЩЕСТВА</b>\n\n"
    medals = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]
    if rows:
        text += "\n".join(f"{medals[i]} <b>@{name}</b> — {votes} голос(ов)" for i, (name, votes) in enumerate(rows))
    else:
        text += "Голосов пока нет — станьте первым!"
    await call.answer()
    await safe_edit(call, text, inline.battle_next_kb())
