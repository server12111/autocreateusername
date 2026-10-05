"""Построители экранов разделов (текст + клавиатура). Используются хэндлерами и после проверки ОП."""

import random

from aiogram import Bot
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from config import REF_TIERS
from database import crud
from database.models import User, utcnow
from keyboards import inline
from texts import MAIN_MENU_TEXT, PROFILE_TEXT, REF_TEXT, SEARCH_TEXT, SHARE_TEXT, SHOP_TEXT

Screen = tuple[str, InlineKeyboardMarkup]

BATTLE_POOL = [
    "crypto", "cyber", "nova", "pixel", "ghost", "alpha", "omega", "royal", "storm", "lunar",
    "neon", "vortex", "zenith", "matrix", "phoenix", "shadow", "titan", "venom", "blaze", "frost",
    "nexus", "orbit", "quantum", "raven", "sonic", "vector", "wolf", "zero", "aurora", "echo",
    "karma", "legend", "mystic", "onyx", "prime", "rogue", "spirit", "unity", "vibe", "zeus",
]


async def safe_edit(event: CallbackQuery | Message, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует сообщение колбэка, а при невозможности — отправляет новое."""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
            return
        except Exception as e:
            if "message is not modified" in str(e):
                return
        await event.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    else:
        await event.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def send_screen(message: Message, screen: Screen) -> None:
    text, kb = screen
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def build_main(session: AsyncSession, user: User, bot: Bot) -> Screen:
    support = await crud.get_setting(session, "support_url")
    battle = await crud.get_setting(session, "battle_enabled") == "1"
    return MAIN_MENU_TEXT, inline.main_menu_kb(support, battle)


def cooldown_left(user: User, cooldown: int) -> int:
    if crud.premium_active(user) or not user.last_search_at or cooldown <= 0:
        return 0
    passed = (utcnow() - user.last_search_at).total_seconds()
    return max(0, int(cooldown - passed))


async def build_search(session: AsyncSession, user: User, bot: Bot) -> Screen:
    premium = crud.premium_active(user)
    cooldown = await crud.get_setting_int(session, "search_cooldown_sec")
    if premium:
        free_left, cd = "♾ безлимит (Premium)", "0 сек ⚡️"
    else:
        free_left = str(user.free_searches_left)
        left = cooldown_left(user, cooldown)
        cd = f"{cooldown} сек" + (f" (осталось {left} сек)" if left else "")
    text = SEARCH_TEXT.format(
        free_left=free_left, paid_left=user.paid_searches_left, cooldown_status=cd
    )
    bonus = 0
    if not premium and not user.sponsor_bonus_claimed:
        bonus = await crud.get_setting_int(session, "sponsor_bonus")
    return text, inline.search_kb(bonus)


async def build_shop(session: AsyncSession, user: User, bot: Bot) -> Screen:
    return SHOP_TEXT, inline.shop_kb()


async def build_profile(session: AsyncSession, user: User, bot: Bot) -> Screen:
    premium = crud.premium_active(user)
    text = PROFILE_TEXT.format(
        user_id=user.tg_id,
        username=f"@{user.username}" if user.username else "не установлен",
        reg_date=f"{user.registered_at:%d.%m.%Y}",
        premium_status_text="💎 <b>Активен</b>" if premium else "❌ Неактивен",
        premium_until_info=f"⏳ Действует до: <b>{user.premium_until:%d.%m.%Y %H:%M}</b> UTC\n" if premium else "",
        total_searches=user.total_searches_done,
        free_left="♾" if premium else user.free_searches_left,
        paid_left=user.paid_searches_left,
    )
    return text, inline.profile_kb()


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
    marks = ["✅" if count >= need else "" for need, _ in REF_TIERS]
    nxt = next((need for need, _ in REF_TIERS if count < need), None)
    if nxt:
        filled = min(10, round(count / nxt * 10))
        progress = f"Прогресс до следующей награды:\n[{'🟩' * filled}{'⬜️' * (10 - filled)}] {count}/{nxt} чел."
    else:
        progress = "🏆 Все уровни наград получены! Спасибо, что приглашаете друзей."
    text = REF_TEXT.format(
        link=link, ref_count=count, current_tier=current,
        t1=marks[0], t2=marks[1], t3=marks[2], t4=marks[3], progress=progress,
    )
    return text, inline.ref_kb(link, SHARE_TEXT)


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
    return text, inline.battle_kb(i, j, a, b)
