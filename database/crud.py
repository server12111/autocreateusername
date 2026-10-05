from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import DEFAULT_SETTINGS, REF_TIERS
from database.models import (
    BattleVote,
    NicknameTrap,
    Payment,
    Promocode,
    PromocodeActivation,
    SearchHistory,
    Setting,
    SponsorChannel,
    User,
    utcnow,
)

# ───────────────────────── settings ─────────────────────────

_settings_cache: dict[str, str] = {}


async def ensure_default_settings(session: AsyncSession) -> None:
    from config import settings as env

    existing = {s.key: s.value for s in (await session.scalars(select(Setting))).all()}
    for key, value in DEFAULT_SETTINGS.items():
        if key not in existing:
            if key == "tgrass_api_key" and env.TGRASS_API_KEY:
                value = env.TGRASS_API_KEY
            if key == "tgrass_enabled" and env.TGRASS_API_KEY:
                value = "1"
            session.add(Setting(key=key, value=value))
            existing[key] = value
    await session.commit()
    _settings_cache.clear()
    _settings_cache.update(existing)


async def get_setting(session: AsyncSession, key: str) -> str:
    if key in _settings_cache:
        return _settings_cache[key]
    row = await session.get(Setting, key)
    value = row.value if row else DEFAULT_SETTINGS.get(key, "")
    _settings_cache[key] = value
    return value


async def get_setting_int(session: AsyncSession, key: str) -> int:
    try:
        return int(await get_setting(session, key))
    except ValueError:
        return int(DEFAULT_SETTINGS.get(key, "0") or 0)


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
    await session.commit()
    _settings_cache[key] = value


# ───────────────────────── users ─────────────────────────


async def get_user(session: AsyncSession, tg_id: int) -> User | None:
    return await session.scalar(select(User).where(User.tg_id == tg_id))


async def find_user(session: AsyncSession, query: str) -> User | None:
    query = query.strip().lstrip("@")
    if query.lstrip("-").isdigit():
        return await get_user(session, int(query))
    return await session.scalar(select(User).where(func.lower(User.username) == query.lower()))


async def get_or_create_user(session: AsyncSession, tg_user) -> tuple[User, bool]:
    user = await get_user(session, tg_user.id)
    if user:
        changed = False
        if user.username != tg_user.username:
            user.username = tg_user.username
            changed = True
        if user.first_name != (tg_user.first_name or ""):
            user.first_name = tg_user.first_name or ""
            changed = True
        is_tg_premium = bool(getattr(tg_user, "is_premium", False))
        if user.is_tg_premium != is_tg_premium:
            user.is_tg_premium = is_tg_premium
            changed = True
        if user.is_blocked_bot:
            user.is_blocked_bot = False
            changed = True
        if changed:
            await session.commit()
        return user, False

    user = User(
        tg_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name or "",
        lang=(tg_user.language_code or "ru")[:8],
        is_tg_premium=bool(getattr(tg_user, "is_premium", False)),
        free_searches_left=await get_setting_int(session, "daily_free_limit"),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        # Параллельный апдейт уже создал пользователя
        await session.rollback()
        return await get_user(session, tg_user.id), False
    return user, True


def premium_active(user: User) -> bool:
    return bool(user.is_premium and user.premium_until and user.premium_until > utcnow())


async def add_premium_days(session: AsyncSession, user: User, days: int) -> datetime:
    now = utcnow()
    base = user.premium_until if premium_active(user) else now
    user.premium_until = base + timedelta(days=days)
    user.is_premium = True
    await session.commit()
    return user.premium_until


async def remove_premium(session: AsyncSession, user: User) -> None:
    user.is_premium = False
    user.premium_until = None
    await session.commit()


async def expire_premiums(session: AsyncSession) -> list[int]:
    rows = (
        await session.scalars(
            select(User).where(User.is_premium.is_(True), User.premium_until <= utcnow())
        )
    ).all()
    for u in rows:
        u.is_premium = False
    await session.commit()
    return [u.tg_id for u in rows]


async def reset_daily_limits(session: AsyncSession) -> None:
    limit = await get_setting_int(session, "daily_free_limit")
    await session.execute(
        update(User).where(User.free_searches_left < limit).values(free_searches_left=limit)
    )
    await session.commit()


async def credit_referral(session: AsyncSession, user: User) -> tuple[User, int | None] | None:
    """Засчитывает реферала. Возвращает (реферер, дни награды | None) или None."""
    if user.is_ref_counted or not user.referrer_id or not user.is_captcha_passed:
        return None
    user.is_ref_counted = True
    referrer = await get_user(session, user.referrer_id)
    if not referrer:
        await session.commit()
        return None
    referrer.referrals_count += 1
    await session.commit()
    reward = next((days for need, days in REF_TIERS if need == referrer.referrals_count), None)
    if reward:
        await add_premium_days(session, referrer, reward)
    return referrer, reward


# ───────────────────────── searches ─────────────────────────


async def add_search(
    session: AsyncSession,
    user_id: int,
    username: str,
    tg_ok: bool,
    fragment_ok: bool,
    status: str,
) -> SearchHistory:
    row = SearchHistory(
        user_id=user_id,
        username_query=username,
        is_available_tg=tg_ok,
        is_available_fragment=fragment_ok,
        status_detail=status,
    )
    session.add(row)
    await session.commit()
    return row


async def save_finding(session: AsyncSession, user_id: int, search_id: int) -> bool:
    row = await session.get(SearchHistory, search_id)
    if not row or row.user_id != user_id:
        return False
    row.is_saved = True
    await session.commit()
    return True


async def get_findings(session: AsyncSession, user_id: int, limit: int = 10) -> list[SearchHistory]:
    return list(
        (
            await session.scalars(
                select(SearchHistory)
                .where(SearchHistory.user_id == user_id, SearchHistory.status_detail == "free")
                .order_by(SearchHistory.is_saved.desc(), SearchHistory.created_at.desc())
                .limit(limit)
            )
        ).all()
    )


async def recently_checked_by_user(session: AsyncSession, user_id: int, since_hours: int = 24) -> set[str]:
    rows = await session.scalars(
        select(SearchHistory.username_query).where(
            SearchHistory.user_id == user_id,
            SearchHistory.created_at >= utcnow() - timedelta(hours=since_hours),
        )
    )
    return set(rows.all())


# ───────────────────────── traps ─────────────────────────


async def add_trap(session: AsyncSession, user_id: int, username: str) -> NicknameTrap | None:
    exists = await session.scalar(
        select(NicknameTrap).where(
            NicknameTrap.user_id == user_id,
            func.lower(NicknameTrap.target_username) == username.lower(),
            NicknameTrap.is_active.is_(True),
        )
    )
    if exists:
        return None
    trap = NicknameTrap(user_id=user_id, target_username=username)
    session.add(trap)
    await session.commit()
    return trap


async def get_user_traps(session: AsyncSession, user_id: int) -> list[NicknameTrap]:
    return list(
        (
            await session.scalars(
                select(NicknameTrap)
                .where(NicknameTrap.user_id == user_id, NicknameTrap.is_active.is_(True))
                .order_by(NicknameTrap.created_at)
            )
        ).all()
    )


async def get_active_traps(session: AsyncSession) -> list[NicknameTrap]:
    return list((await session.scalars(select(NicknameTrap).where(NicknameTrap.is_active.is_(True)))).all())


async def delete_trap(session: AsyncSession, user_id: int, trap_id: int) -> None:
    await session.execute(
        update(NicknameTrap)
        .where(NicknameTrap.id == trap_id, NicknameTrap.user_id == user_id)
        .values(is_active=False)
    )
    await session.commit()


# ───────────────────────── sponsors ─────────────────────────


async def get_sponsors(session: AsyncSession, only_active: bool = True) -> list[SponsorChannel]:
    q = select(SponsorChannel).order_by(SponsorChannel.priority, SponsorChannel.id)
    if only_active:
        q = q.where(SponsorChannel.is_active.is_(True))
    return list((await session.scalars(q)).all())


async def add_sponsor(session: AsyncSession, title: str, channel_id: int, invite_link: str, priority: int = 0) -> SponsorChannel:
    ch = SponsorChannel(title=title, channel_id=channel_id, invite_link=invite_link, priority=priority)
    session.add(ch)
    await session.commit()
    return ch


async def toggle_sponsor(session: AsyncSession, sponsor_id: int) -> SponsorChannel | None:
    ch = await session.get(SponsorChannel, sponsor_id)
    if ch:
        ch.is_active = not ch.is_active
        await session.commit()
    return ch


async def delete_sponsor(session: AsyncSession, sponsor_id: int) -> None:
    await session.execute(delete(SponsorChannel).where(SponsorChannel.id == sponsor_id))
    await session.commit()


# ───────────────────────── promocodes ─────────────────────────


async def create_promocode(
    session: AsyncSession, code: str, reward_type: str, value: int, max_act: int, days_valid: int | None
) -> Promocode | None:
    if await session.scalar(select(Promocode).where(func.lower(Promocode.code) == code.lower())):
        return None
    promo = Promocode(
        code=code,
        reward_type=reward_type,
        reward_value=value,
        max_activations=max_act,
        expires_at=utcnow() + timedelta(days=days_valid) if days_valid else None,
    )
    session.add(promo)
    await session.commit()
    return promo


async def list_promocodes(session: AsyncSession) -> list[Promocode]:
    return list((await session.scalars(select(Promocode).order_by(Promocode.id.desc()).limit(30))).all())


async def deactivate_promocode(session: AsyncSession, promo_id: int) -> None:
    await session.execute(update(Promocode).where(Promocode.id == promo_id).values(is_active=False))
    await session.commit()


async def activate_promocode(session: AsyncSession, user: User, code: str) -> tuple[bool, str]:
    promo = await session.scalar(select(Promocode).where(func.lower(Promocode.code) == code.strip().lower()))
    if not promo or not promo.is_active:
        return False, "❌ Промокод не найден или отключён."
    if promo.expires_at and promo.expires_at < utcnow():
        return False, "⌛️ Срок действия промокода истёк."
    if promo.activations_count >= promo.max_activations:
        return False, "😔 Лимит активаций промокода исчерпан."
    used = await session.scalar(
        select(PromocodeActivation).where(
            PromocodeActivation.promocode_id == promo.id, PromocodeActivation.user_id == user.tg_id
        )
    )
    if used:
        return False, "⚠️ Вы уже активировали этот промокод."

    promo.activations_count += 1
    session.add(PromocodeActivation(promocode_id=promo.id, user_id=user.tg_id))
    if promo.reward_type == "premium_days":
        until = await add_premium_days(session, user, promo.reward_value)
        return True, (
            f"✅ Промокод активирован!\n\n💎 Начислено: <b>+{promo.reward_value} дн. Premium</b>\n"
            f"Premium активен до: <b>{until:%d.%m.%Y %H:%M}</b> UTC"
        )
    user.paid_searches_left += promo.reward_value
    await session.commit()
    return True, f"✅ Промокод активирован!\n\n🔍 Начислено: <b>+{promo.reward_value} поисков</b>"


# ───────────────────────── payments ─────────────────────────


async def add_payment(session: AsyncSession, user_id: int, payload: str, amount: int, charge_id: str) -> bool:
    if await session.scalar(select(Payment).where(Payment.charge_id == charge_id)):
        return False
    session.add(Payment(user_id=user_id, payload=payload, amount=amount, charge_id=charge_id))
    await session.commit()
    return True


# ───────────────────────── battle ─────────────────────────


async def add_battle_vote(session: AsyncSession, user_id: int, winner: str, loser: str) -> bool:
    """Один голос пользователя на пару ников. False — уже голосовал."""
    voted = await session.scalar(
        select(BattleVote.id).where(
            BattleVote.user_id == user_id,
            or_(
                (BattleVote.winner == winner) & (BattleVote.loser == loser),
                (BattleVote.winner == loser) & (BattleVote.loser == winner),
            ),
        )
    )
    if voted:
        return False
    session.add(BattleVote(user_id=user_id, winner=winner, loser=loser))
    await session.commit()
    return True


async def battle_pair_stats(session: AsyncSession, a: str, b: str) -> tuple[int, int]:
    async def count(w: str, l: str) -> int:
        return await session.scalar(
            select(func.count()).select_from(BattleVote).where(BattleVote.winner == w, BattleVote.loser == l)
        ) or 0

    return await count(a, b), await count(b, a)


async def battle_top(session: AsyncSession, limit: int = 10) -> list[tuple[str, int]]:
    rows = await session.execute(
        select(BattleVote.winner, func.count().label("c"))
        .group_by(BattleVote.winner)
        .order_by(func.count().desc())
        .limit(limit)
    )
    return [(r[0], r[1]) for r in rows.all()]


# ───────────────────────── stats ─────────────────────────


async def get_stats(session: AsyncSession) -> dict:
    now = utcnow()

    async def count_users(since: datetime | None = None) -> int:
        q = select(func.count()).select_from(User)
        if since:
            q = q.where(User.registered_at >= since)
        return await session.scalar(q) or 0

    return {
        "total": await count_users(),
        "day": await count_users(now - timedelta(days=1)),
        "week": await count_users(now - timedelta(days=7)),
        "month": await count_users(now - timedelta(days=30)),
        "premium": await session.scalar(
            select(func.count()).select_from(User).where(User.is_premium.is_(True), User.premium_until > now)
        )
        or 0,
        "blocked": await session.scalar(
            select(func.count()).select_from(User).where(User.is_blocked_bot.is_(True))
        )
        or 0,
        "searches": await session.scalar(select(func.count()).select_from(SearchHistory)) or 0,
        "found": await session.scalar(
            select(func.count()).select_from(SearchHistory).where(SearchHistory.status_detail == "free")
        )
        or 0,
        "traps": await session.scalar(
            select(func.count()).select_from(NicknameTrap).where(NicknameTrap.is_active.is_(True))
        )
        or 0,
        "stars": await session.scalar(select(func.coalesce(func.sum(Payment.amount), 0))) or 0,
        "payments": await session.scalar(select(func.count()).select_from(Payment)) or 0,
    }


async def all_user_ids(session: AsyncSession) -> list[int]:
    return list(
        (
            await session.scalars(
                select(User.tg_id).where(User.is_banned.is_(False), User.is_blocked_bot.is_(False))
            )
        ).all()
    )


async def mark_blocked(session: AsyncSession, tg_ids: list[int]) -> None:
    if tg_ids:
        await session.execute(update(User).where(User.tg_id.in_(tg_ids)).values(is_blocked_bot=True))
        await session.commit()
