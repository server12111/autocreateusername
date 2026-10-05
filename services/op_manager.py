"""Спонсорские каналы (свои + Tgrass + BotoHub): подписка на них даёт бонусные поиски."""

import logging
from dataclasses import dataclass, field

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from database import crud
from database.models import User
from services.botohub_service import BotohubService
from services.tgrass_service import TgrassService

log = logging.getLogger(__name__)


@dataclass
class OpChannel:
    title: str
    url: str


@dataclass
class SponsorState:
    missing: list[OpChannel] = field(default_factory=list)  # на что ещё не подписан
    available: bool = False  # выдал ли хоть один источник спонсоров

    @property
    def all_done(self) -> bool:
        """Спонсоры есть и пользователь подписан на всех — можно давать бонус."""
        return self.available and not self.missing


async def check_sponsors(bot: Bot, session: AsyncSession, user: User) -> SponsorState:
    state = SponsorState()

    # Собственные каналы администратора
    for ch in await crud.get_sponsors(session):
        try:
            member = await bot.get_chat_member(ch.channel_id, user.tg_id)
        except Exception as e:
            # Бот не админ в канале или канал удалён — такой канал не учитываем
            log.warning("ОП: не удалось проверить канал %s (%s): %s", ch.title, ch.channel_id, e)
            continue
        state.available = True
        if member.status in ("left", "kicked"):
            state.missing.append(OpChannel(ch.title, ch.invite_link))

    # Спонсоры Tgrass
    if await crud.get_setting(session, "tgrass_enabled") == "1":
        key = await crud.get_setting(session, "tgrass_api_key")
        if key:
            offers, has = await TgrassService(key).get_offers(
                user.tg_id, user.username, user.lang, user.is_tg_premium
            )
            state.available |= has
            for o in offers:
                state.missing.append(OpChannel(o.get("name") or "Канал спонсора", o["link"]))

    # Спонсоры BotoHub (названий не отдаёт — нумеруем, чтобы кнопки различались)
    if await crud.get_setting(session, "botohub_enabled") == "1":
        key = await crud.get_setting(session, "botohub_api_key")
        if key:
            links, has = await BotohubService(key).get_unsubscribed(user.tg_id)
            state.available |= has
            for url in links:
                state.missing.append(OpChannel(f"Спонсор #{len(state.missing) + 1}", url))
    return state


async def get_unsubscribed(bot: Bot, session: AsyncSession, user: User) -> list[OpChannel]:
    return (await check_sponsors(bot, session, user)).missing


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
