"""Построители экранов разделов (текст + клавиатура). Используются хэндлерами и после проверки ОП."""

import random

from aiogram import Bot
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from config import REF_TIERS, plural
from database import crud
from database.models import User, msk, utcnow
from keyboards import inline
from services.banners import preview
from texts import MAIN_MENU_TEXT, PROFILE_TEXT, REF_TEXT, SEARCH_TEXT, SHARE_TEXT, SHOP_TEXT

# (текст, клавиатура, баннер) — баннер из services/banners.py или None; админские экраны — без баннера
Screen = tuple[str, InlineKeyboardMarkup, str | None]

BATTLE_POOL = [
    "crypto", "cyber", "nova", "pixel", "ghost", "alpha", "omega", "royal", "storm", "lunar",
    "neon", "vortex", "zenith", "matrix", "phoenix", "shadow", "titan", "venom", "blaze", "frost",
    "nexus", "orbit", "quantum", "raven", "sonic", "vector", "wolf", "zero", "aurora", "echo",
    "karma", "legend", "mystic", "onyx", "prime", "rogue", "spirit", "unity", "vibe", "zeus",
]


async def safe_edit(
    event: CallbackQuery | Message, text: str, kb: InlineKeyboardMarkup | None = None, banner: str | None = None
) -> None:
    """Редактирует сообщение колбэка, а при невозможности — отправляет новое. banner — баннер раздела над текстом."""
    lp = preview(banner)
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb, link_preview_options=lp)
            return
        except Exception as e:
            if "message is not modified" in str(e):
                return
        await event.message.answer(text, reply_markup=kb, link_preview_options=lp)
    else:
        await event.answer(text, reply_markup=kb, link_preview_options=lp)


async def send_screen(message: Message, screen: tuple) -> None:
    text, kb, *rest = screen
    await message.answer(text, reply_markup=kb, link_preview_options=preview(rest[0] if rest else None))


async def build_main(session: AsyncSession, user: User, bot: Bot) -> Screen:
    support = await crud.get_setting(session, "support_url")
    battle = await crud.get_setting(session, "battle_enabled") == "1"
    return MAIN_MENU_TEXT, inline.main_menu_kb(support, battle), "main"


def cooldown_left(user: User, cooldown: int) -> int:
    if crud.premium_active(user) or not user.last_search_at or cooldown <= 0:
        return 0
    passed = (utcnow() - user.last_search_at).total_seconds()
    return max(0, int(cooldown - passed))


async def build_search(session: AsyncSession, user: User, bot: Bot) -> Screen:
    premium = crud.premium_active(user)
    cooldown = await crud.get_setting_int(session, "search_cooldown_sec")
    if premium:
        daily = await crud.premium_daily_left(session, user)
        free_left = f"💎 Premium — сегодня осталось {daily[0]} из {daily[1]}" if daily else "♾ безлимит (Premium)"
        cd = "0 сек ⚡️"
    else:
        free_left = str(user.free_searches_left)
        left = cooldown_left(user, cooldown)
        cd = f"{cooldown} сек" + (f" (осталось {left} сек)" if left else "")
    text = SEARCH_TEXT.format(
        free_left=free_left, paid_left=user.paid_searches_left, cooldown_status=cd
    )
    bonus = 0
    # Бонус дают только Tgrass/BotoHub (свои каналы — обязательные): без них кнопка вела бы в тупик
    if not premium and not user.sponsor_bonus_claimed and await crud.bonus_sponsors_enabled(session):
        bonus = await crud.get_setting_int(session, "sponsor_bonus")
    five_free = crud.first_search_free(user)
    return text, inline.search_kb(bonus, five_free), "search"


async def premium_limit_phrase(session: AsyncSession) -> str:
    """«до 25 юзернеймов в день» или «без лимита», если лимит выключен в админке."""
    limit = await crud.get_setting_int(session, "premium_daily_limit")
    return f"до {limit} юзернеймов в день" if limit > 0 else "без лимита"


async def build_shop(session: AsyncSession, user: User, bot: Bot) -> Screen:
    limit = await crud.get_setting_int(session, "premium_daily_limit")
    line = f"{limit} {plural(limit, 'поиск', 'поиска', 'поисков')} в день" if limit > 0 else "Безлимитный поиск"
    return SHOP_TEXT.format(limit=line), inline.shop_kb(), "premium"


async def build_profile(session: AsyncSession, user: User, bot: Bot) -> Screen:
    premium = crud.premium_active(user)
    free_left = user.free_searches_left
    if premium:
        daily = await crud.premium_daily_left(session, user)
        free_left = f"сегодня {daily[0]} из {daily[1]} (Premium)" if daily else "♾"
    text = PROFILE_TEXT.format(
        user_id=user.tg_id,
        username=f"@{user.username}" if user.username else "не установлен",
        reg_date=f"{msk(user.registered_at):%d.%m.%Y}",
        premium_status_text="💎 <b>Активен</b>" if premium else "❌ Неактивен",
        premium_until_info=f"⏳ Действует до: <b>{msk(user.premium_until):%d.%m.%Y %H:%M}</b> МСК\n" if premium else "",
        total_searches=user.total_searches_done,
        free_left=free_left,
        paid_left=user.paid_searches_left,
    )
    return text, inline.profile_kb(), "profile"


def ref_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


async def build_ref(session: AsyncSession, user: User, bot: Bot) -> Screen:
    me = await bot.me()
    link = ref_link(me.username, user.tg_id)
    count = user.referrals_count
    tier_names = ["🥉 Бронза", "🥈 Серебро", "🥇 Золото", "💎 Бриллиант"]
    current = "—"
    for (need, _), name in zip(REF_TIERS, tier_names):
        if count >= need:
            current = name
    marks = [" ✅" if count >= need else "" for need, _ in REF_TIERS]
    nxt = next((need for need, _ in REF_TIERS if count < need), None)
    if nxt:
        filled = min(10, round(count / nxt * 10))
        progress = f"{'🟩' * filled}{'⬜️' * (10 - filled)} {count}/{nxt}"
    else:
        progress = "🏆 Все награды получены!"
    text = REF_TEXT.format(
        link=link, ref_count=count, current_tier=current,
        t1=marks[0], t2=marks[1], t3=marks[2], t4=marks[3], progress=progress,
    )
    return text, inline.ref_kb(link, SHARE_TEXT), "ref"


async def build_battle(session: AsyncSession, user: User, bot: Bot) -> Screen:
    i, j = random.sample(range(len(BATTLE_POOL)), 2)
    a, b = BATTLE_POOL[i], BATTLE_POOL[j]
    text = (
        "⚔️ <b>БИТВА НИКНЕЙМОВ</b>\n"
        "\n"
        "Какой юзернейм красивее и статуснее?\n\n"
        f"🔴 <b>@{a}</b>\n        vs\n🔵 <b>@{b}</b>\n\n"
        "Проголосуйте кнопкой ниже 👇"
    )
    return text, inline.battle_kb(i, j, a, b), "battle"
