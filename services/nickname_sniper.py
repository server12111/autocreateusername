import asyncio
import logging
from collections import defaultdict

from aiogram import Bot

from database import crud
from database.base import session_maker
from database.models import utcnow
from services import social_checker
from services.username_checker import UsernameChecker

log = logging.getLogger(__name__)

# Где и как занять освободившийся ник (сам ник подставляется в {name})
CLAIM_HINT = {
    "tg": "Займите его на свой аккаунт или канал:\n👉 https://t.me/{name}",
    "yt": "Укажите его как псевдоним канала: YouTube → Настройки канала → Псевдоним\n👉 https://www.youtube.com/handle",
    "x": "Смените имя пользователя: X → Настройки → Аккаунт → Имя пользователя\n👉 https://x.com/settings/screen_name",
    "tt": "Смените имя пользователя в приложении TikTok: Профиль → Изменить профиль → Имя пользователя",
}


def trap_networks(trap) -> list[str]:
    """Сети, в которых ловушка ещё ждёт освобождения ника."""
    freed = set(filter(None, (trap.freed or "").split(",")))
    return [n for n in (trap.networks or "tg").split(",") if n and n not in freed]


async def _check(checker: UsernameChecker, net: str, name: str) -> bool:
    """True — ник точно свободен в сети. Сомнительные ответы (unknown) свободным не считаем."""
    if net == "tg":
        return (await checker.check(name)).is_free
    return await social_checker.check(net, name, use_cache=False) == "free"


async def run_sniper_cycle(bot: Bot, checker: UsernameChecker) -> None:
    """Один проход «Ловушки на ник»: проверяет все активные ловушки Premium-пользователей."""
    async with session_maker() as session:
        traps = await crud.get_active_traps(session)
        if not traps:
            return

        by_target = defaultdict(list)  # (сеть, ник) -> ловушки
        for trap in traps:
            user = await crud.get_user(session, trap.user_id)
            if not user or user.is_banned or not crud.premium_active(user):
                continue
            for net in trap_networks(trap):
                by_target[(net, trap.target_username.lower())].append(trap)

        targets = list(by_target)
        free: set[tuple[str, str]] = set()
        for i in range(0, len(targets), 10):
            chunk = targets[i : i + 10]
            results = await asyncio.gather(*(_check(checker, net, name) for net, name in chunk), return_exceptions=True)
            free |= {t for t, ok in zip(chunk, results) if ok is True}

        now = utcnow()
        for trap in {t for ts in by_target.values() for t in ts}:
            trap.last_checked_at = now
        for net, name in free:
            for trap in by_target[(net, name)]:
                trap.freed = ",".join(filter(None, [trap.freed, net]))
                if not trap_networks(trap):
                    trap.is_active = False
                    trap.notified_at = now
                await _notify(bot, trap.user_id, net, trap.target_username)
        await session.commit()


async def _notify(bot: Bot, user_id: int, net: str, name: str) -> None:
    where = social_checker.icon(net) + " " + social_checker.ALL[net].title
    try:
        await bot.send_message(
            user_id,
            "🚨 <b>ВНИМАНИЕ! НИКНЕЙМ ОСВОБОДИЛСЯ!</b> 🚨\n\n"
            f"Юзернейм <b>@{name}</b> прямо сейчас свободен в {where}!\n\n"
            "⚡️ " + CLAIM_HINT[net].format(name=name),
            disable_notification=False,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.info("Снайпер: не удалось уведомить %s: %s", user_id, e)
