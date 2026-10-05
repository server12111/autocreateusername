"""Спонсорские каналы (свои + Tgrass): подписка на них даёт бонусные поиски."""

import logging
from dataclasses import dataclass

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from database import crud
from database.models import User
from services.tgrass_service import TgrassService

log = logging.getLogger(__name__)


@dataclass
class OpChannel:
    title: str
    url: str


async def get_unsubscribed(bot: Bot, session: AsyncSession, user: User) -> list[OpChannel]:
    """Каналы спонсоров, на которые пользователь ещё не подписан. Пустой список — подписан на всё."""
    result: list[OpChannel] = []

    # Собственные каналы администратора
    for ch in await crud.get_sponsors(session):
        try:
            member = await bot.get_chat_member(ch.channel_id, user.tg_id)
            if member.status in ("left", "kicked"):
                result.append(OpChannel(ch.title, ch.invite_link))
        except Exception as e:
            # Бот не админ в канале или канал удалён — не блокируем пользователя
            log.warning("ОП: не удалось проверить канал %s (%s): %s", ch.title, ch.channel_id, e)

    # Спонсоры Tgrass
    if await crud.get_setting(session, "tgrass_enabled") == "1":
        key = await crud.get_setting(session, "tgrass_api_key")
        if key:
            offers = await TgrassService(key).get_offers(
                user.tg_id, user.username, user.lang, user.is_tg_premium
            )
            for o in offers:
                result.append(OpChannel(o.get("name") or "Канал спонсора", o["link"]))
    return result


async def notify_referrer(bot: Bot, session: AsyncSession, user: User) -> None:
    """Засчитывает приглашённого друга (после подписки на спонсоров) и уведомляет пригласившего."""
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
