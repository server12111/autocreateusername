import asyncio
import logging
from collections import defaultdict

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import crud
from database.base import session_maker
from database.models import utcnow
from services.username_checker import UsernameChecker

log = logging.getLogger(__name__)


async def run_sniper_cycle(bot: Bot, checker: UsernameChecker) -> None:
    """Один проход «Ловушки на ник»: проверяет все активные ловушки Premium-пользователей."""
    async with session_maker() as session:
        traps = await crud.get_active_traps(session)
        if not traps:
            return

        by_name = defaultdict(list)
        for trap in traps:
            user = await crud.get_user(session, trap.user_id)
            if user and not user.is_banned and crud.premium_active(user):
                by_name[trap.target_username.lower()].append(trap)

        names = list(by_name)
        results = []
        for i in range(0, len(names), 10):
            results += await asyncio.gather(*(checker.check(n) for n in names[i : i + 10]))

        now = utcnow()
        for res in results:
            for trap in by_name[res.username.lower()]:
                trap.last_checked_at = now
                if not res.is_free:
                    continue
                trap.is_active = False
                trap.notified_at = now
                kb = InlineKeyboardBuilder()
                kb.button(text="🚀 Занять никнейм", url=f"https://t.me/{res.username}")
                try:
                    await bot.send_message(
                        trap.user_id,
                        "🚨 <b>ВНИМАНИЕ! НИКНЕЙМ ОСВОБОДИЛСЯ!</b> 🚨\n\n"
                        f"Желанный юзернейм <b>@{res.username}</b> прямо сейчас стал свободен!\n"
                        "⚡️ Скорее перейдите и займите его на свой аккаунт или канал:\n"
                        f"👉 https://t.me/{res.username}",
                        reply_markup=kb.as_markup(),
                        disable_notification=False,
                    )
                except Exception as e:
                    log.info("Снайпер: не удалось уведомить %s: %s", trap.user_id, e)
        await session.commit()
