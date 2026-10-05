import logging
import time
from dataclasses import dataclass

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from database import crud
from database.models import User
from services.tgrass_service import TgrassService

log = logging.getLogger(__name__)

# Кэш успешной проверки, чтобы не дёргать API на каждый клик
PASS_CACHE_TTL = 90
_passed: dict[int, float] = {}


@dataclass
class OpChannel:
    title: str
    url: str


async def get_unsubscribed(bot: Bot, session: AsyncSession, user: User) -> list[OpChannel]:
    result: list[OpChannel] = []

    # Шаг 1: собственные каналы администратора
    for ch in await crud.get_sponsors(session):
        try:
            member = await bot.get_chat_member(ch.channel_id, user.tg_id)
            if member.status in ("left", "kicked"):
                result.append(OpChannel(ch.title, ch.invite_link))
        except Exception as e:
            # Бот не админ в канале или канал удалён — не блокируем пользователя
            log.warning("ОП: не удалось проверить канал %s (%s): %s", ch.title, ch.channel_id, e)

    # Шаг 2: спонсоры Tgrass
    if await crud.get_setting(session, "tgrass_enabled") == "1":
        key = await crud.get_setting(session, "tgrass_api_key")
        if key:
            offers = await TgrassService(key).get_offers(
                user.tg_id, user.username, user.lang, user.is_tg_premium
            )
            for o in offers:
                result.append(OpChannel(o.get("name") or "Канал спонсора", o["link"]))
    return result


async def check_subscription(bot: Bot, session: AsyncSession, user: User, use_cache: bool = True) -> list[OpChannel]:
    """Пустой список — пользователь подписан на всё."""
    if use_cache and _passed.get(user.tg_id, 0) > time.time():
        return []
    missing = await get_unsubscribed(bot, session, user)
    if missing:
        _passed.pop(user.tg_id, None)
    else:
        _passed[user.tg_id] = time.time() + PASS_CACHE_TTL
        await on_subscription_passed(bot, session, user)
    return missing


async def on_subscription_passed(bot: Bot, session: AsyncSession, user: User) -> None:
    """Засчитывает реферала после капчи и ОП."""
    credited = await crud.credit_referral(session, user)
    if not credited:
        return
    referrer, reward = credited
    text = (
        f"👥 По вашей ссылке присоединился новый друг!\n"
        f"Всего активных приглашённых: <b>{referrer.referrals_count}</b>"
    )
    if reward:
        text += f"\n\n🎁 Достигнут новый уровень! Начислено <b>+{reward} дн. Premium</b>."
    try:
        await bot.send_message(referrer.tg_id, text)
    except Exception:
        pass
