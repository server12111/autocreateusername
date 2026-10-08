"""Спонсорские каналы.

Свои каналы администратора — обязательная подписка: без неё ботом пользоваться нельзя.
Tgrass и BotoHub — по желанию, подписка на них даёт бонусные поиски.
"""

import html
import logging
import time
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config import REF_BURST_COUNT, REF_BURST_MINUTES, settings
from database import crud
from database.models import User
from services.botohub_service import BotohubService
from services.tgrass_service import TgrassService

log = logging.getLogger(__name__)

MAX_SHOWN = 10  # сколько спонсоров показывать за раз; остальные появятся после подписки на эти
REQUIRED_OK_TTL = 300  # сек: подписку на обязательные каналы перепроверяем не чаще раза в 5 минут
_required_ok_until: dict[int, float] = {}


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


async def check_required(bot: Bot, session: AsyncSession, user: User, use_cache: bool = True) -> list[OpChannel]:
    """Свои каналы администратора — обязательная подписка. Возвращает те, на которые пользователь
    не подписан (не больше MAX_SHOWN). Пустой список — можно пользоваться ботом."""
    if use_cache and _required_ok_until.get(user.tg_id, 0) > time.time():
        return []
    missing = []
    for ch in await crud.get_sponsors(session):
        try:
            member = await bot.get_chat_member(ch.channel_id, user.tg_id)
        except Exception as e:
            # Бот не админ в канале или канал удалён — такой канал не учитываем, иначе бот станет недоступен всем
            log.warning("ОП: не удалось проверить канал %s (%s): %s", ch.title, ch.channel_id, e)
            continue
        if member.status in ("left", "kicked"):
            missing.append(OpChannel(ch.title, ch.invite_link))
            if len(missing) >= MAX_SHOWN:
                break
    if not missing:
        _required_ok_until[user.tg_id] = time.time() + REQUIRED_OK_TTL
    return missing


def reset_required_cache() -> None:
    """Сбросить кэш проверок — например, после добавления нового обязательного канала."""
    _required_ok_until.clear()


async def check_sponsors(bot: Bot, session: AsyncSession, user: User) -> SponsorState:
    """Спонсоры за бонусные поиски: Tgrass и BotoHub (свои каналы — обязательные, см. check_required)."""
    state = SponsorState()

    # Спонсоры Tgrass
    if len(state.missing) < MAX_SHOWN and await crud.get_setting(session, "tgrass_enabled") == "1":
        key = await crud.get_setting(session, "tgrass_api_key")
        if key:
            offers, has = await TgrassService(key).get_offers(
                user.tg_id, user.username, user.lang, user.is_tg_premium
            )
            state.available |= has
            for o in offers:
                state.missing.append(OpChannel(o.get("name") or "Канал спонсора", o["link"]))

    # Спонсоры BotoHub (названий не отдаёт — нумеруем, чтобы кнопки различались)
    if len(state.missing) < MAX_SHOWN and await crud.get_setting(session, "botohub_enabled") == "1":
        key = await crud.get_setting(session, "botohub_api_key")
        if key:
            links, has = await BotohubService(key).get_unsubscribed(user.tg_id)
            state.available |= has
            for url in links:
                state.missing.append(OpChannel(f"Спонсор #{len(state.missing) + 1}", url))
    # Не больше MAX_SHOWN за раз. Бонус всё равно дают только после подписки на всех:
    # пока список не пуст, all_done ложно, а следующие спонсоры покажутся при новой проверке
    state.missing = state.missing[:MAX_SHOWN]
    return state


async def get_unsubscribed(bot: Bot, session: AsyncSession, user: User) -> list[OpChannel]:
    return (await check_sponsors(bot, session, user)).missing


async def notify_referrer(bot: Bot, session: AsyncSession, user: User) -> None:
    """Засчитывает приглашённого друга (после капчи, подписки и первого поиска) и уведомляет пригласившего.
    Если у пригласившего подозрительно много друзей за последние минуты — задерживает друга до решения админа."""
    if not crud.referral_ready(user):
        return
    held = await crud.hold_referral_if_burst(session, user)
    if held:
        # Админам — при первом задержанном и дальше на каждом пятом, чтобы не засыпать сообщениями
        if held == 1 or held % 5 == 0:
            await _alert_admins(bot, session, user.referrer_id)
        return
    credited = await crud.credit_referral(session, user)
    if credited:
        await _send_credited(bot, *credited)


async def approve_held_referrals(bot: Bot, session: AsyncSession, referrer_id: int) -> int:
    """Админ одобрил задержанных друзей: засчитываем каждого и выдаём награды."""
    approved = 0
    for friend in await crud.held_referrals(session, referrer_id):
        credited = await crud.credit_referral(session, friend, approved=True)
        if credited:
            approved += 1
            await _send_credited(bot, *credited)
    return approved


async def _alert_admins(bot: Bot, session: AsyncSession, referrer_id: int) -> None:
    referrer = await crud.get_user(session, referrer_id)
    held = await crud.held_referrals(session, referrer_id)
    if not referrer or not held:
        return
    who = f"@{referrer.username}" if referrer.username else html.escape(referrer.first_name or "без имени")
    lines = "\n".join(
        f"• {'@' + f.username if f.username else '—'} · {html.escape(f.first_name or '')} · "
        f"<code>{f.tg_id}</code> · поисков {f.total_searches_done}"
        for f in held[:15]
    )
    more = f"\n…и ещё {len(held) - 15}" if len(held) > 15 else ""
    text = (
        "🚨 <b>Похоже на накрутку рефералов</b>\n\n"
        f"{who} (<code>{referrer.tg_id}</code>): за {REF_BURST_MINUTES} мин засчитано "
        f"{REF_BURST_COUNT}+ друзей подряд, следующие задержаны\n"
        f"Друзей засчитано: <b>{referrer.referrals_count}</b> · задержано: <b>{len(held)}</b>\n\n"
        f"{lines}{more}\n\n"
        "<i>Кнопки действуют на всех задержанных на момент нажатия</i>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Засчитать", callback_data=f"adm:ref:ok:{referrer_id}")
    kb.button(text="❌ Не засчитывать", callback_data=f"adm:ref:no:{referrer_id}")
    kb.adjust(2)
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=kb.as_markup())
        except Exception as e:
            log.info("Админ %s не получил уведомление о накрутке: %r", admin_id, e)


async def _send_credited(bot: Bot, referrer: User, reward: int | None) -> None:
    text = (
        f"👥 По вашей ссылке присоединился новый друг!\n"
        f"Всего активных приглашённых: <b>{referrer.referrals_count}</b>"
    )
    if reward:
        text += f"\n\n🎁 Достигнут новый уровень! Начислено <b>+{reward} дн. Premium</b>"
    try:
        await bot.send_message(referrer.tg_id, text)
    except Exception:
        pass
