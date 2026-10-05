from urllib.parse import quote

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import PREMIUM_PLANS, SEARCH_PACKS


def _back(kb: InlineKeyboardBuilder, data: str = "menu:main", text: str = "🔙 Главное меню") -> None:
    kb.button(text=text, callback_data=data)


def captcha_kb(options: list[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for opt in options:
        kb.button(text=str(opt), callback_data=f"cap:{opt}")
    kb.adjust(3)
    return kb.as_markup()


def main_menu_kb(support_url: str, battle_enabled: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Поиск", callback_data="menu:search")
    kb.button(text="🛒 Магазин", callback_data="menu:shop")
    kb.button(text="👤 Профиль", callback_data="menu:profile")
    kb.button(text="👥 Рефералы", callback_data="menu:ref")
    if battle_enabled:
        kb.button(text="⚔️ Битва Никнеймов", callback_data="menu:battle")
    kb.button(text="🆘 Поддержка", url=support_url or "https://t.me/")
    kb.adjust(2)
    return kb.as_markup()


def sponsor_bonus_kb(channels, bonus: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for ch in channels:
        kb.button(text=f"📢 {ch.title}"[:64], url=ch.url)
    if channels:
        kb.button(text="✅ Проверить подписку", callback_data="check_op_sub")
    else:
        kb.button(text=f"🎁 Забрать +{bonus} поиска", callback_data="check_op_sub")
    kb.button(text="💎 Купить Premium", callback_data="shop:premium")
    kb.button(text="🔙 Назад в поиск", callback_data="menu:search")
    kb.adjust(1)
    return kb.as_markup()


def search_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💎 5 букв (Редкие)", callback_data="s:5")
    kb.button(text="🔤 6 букв", callback_data="s:6")
    kb.button(text="🎯 Поиск по фильтру/маске", callback_data="s:mask")
    kb.button(text="🪤 Ловушка на ник (Снайпер)", callback_data="s:trap")
    _back(kb)
    kb.adjust(2, 1, 1, 1)
    return kb.as_markup()


def found_kb(username: str, mode: str, search_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Занять никнейм", url=f"https://t.me/{username}")
    kb.button(text="🔄 Искать ещё", callback_data=f"s:{mode}")
    kb.button(text="📁 В мои находки", callback_data=f"s:save:{search_id}")
    _back(kb, "menu:search", "🔙 Назад в поиск")
    kb.adjust(1, 2, 1)
    return kb.as_markup()


def retry_kb(mode: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Попробовать ещё", callback_data=f"s:{mode}")
    _back(kb, "menu:search", "🔙 Назад в поиск")
    kb.adjust(1)
    return kb.as_markup()


def premium_only_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐️ Купить Premium", callback_data="shop:premium")
    kb.button(text="👥 Получить за друзей", callback_data="menu:ref")
    _back(kb, "menu:search", "🔙 Назад в поиск")
    kb.adjust(1)
    return kb.as_markup()


def cancel_kb(back: str = "menu:search") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=back)
    return kb.as_markup()


def traps_kb(traps) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for t in traps:
        kb.button(text=f"🗑 @{t.target_username}", callback_data=f"trap:del:{t.id}")
    kb.button(text="➕ Добавить ловушку", callback_data="trap:add")
    _back(kb, "menu:search", "🔙 Назад в поиск")
    kb.adjust(1)
    return kb.as_markup()


def shop_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐️ Купить Premium", callback_data="shop:premium")
    kb.button(text="📦 Пакеты поисков", callback_data="shop:packs")
    kb.button(text="👥 Пригласить друзей", callback_data="menu:ref")
    _back(kb)
    kb.adjust(2, 1, 1)
    return kb.as_markup()


def premium_plans_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, (days, price) in PREMIUM_PLANS.items():
        kb.button(text=f"💎 {days} дн. — {price} ⭐️", callback_data=f"buy:{key}")
    _back(kb, "menu:shop", "🔙 В магазин")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def packs_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, (count, price) in SEARCH_PACKS.items():
        kb.button(text=f"🔍 {count} поисков — {price} ⭐️", callback_data=f"buy:{key}")
    _back(kb, "menu:shop", "🔙 В магазин")
    kb.adjust(1)
    return kb.as_markup()


def profile_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Ввести промокод", callback_data="prof:promo")
    kb.button(text="📁 Мои находки", callback_data="prof:finds")
    kb.button(text="ℹ️ Полезная информация", callback_data="prof:info")
    _back(kb)
    kb.adjust(1, 2, 1)
    return kb.as_markup()


def back_kb(data: str = "menu:main", text: str = "🔙 Назад") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _back(kb, data, text)
    return kb.as_markup()


def ref_kb(link: str, share_text: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🚀 Поделиться ссылкой",
        url=f"https://t.me/share/url?url={quote(link)}&text={quote(share_text)}",
    )
    _back(kb)
    kb.adjust(1)
    return kb.as_markup()


def battle_kb(i: int, j: int, a: str, b: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"👑 @{a}", callback_data=f"bt:v:{i}:{j}")
    kb.button(text=f"👑 @{b}", callback_data=f"bt:v:{j}:{i}")
    kb.button(text="⏭ Пропустить", callback_data="menu:battle")
    kb.button(text="🏆 ТОП-10", callback_data="bt:top")
    _back(kb)
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def battle_next_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⚔️ Следующая битва", callback_data="menu:battle")
    kb.button(text="🏆 ТОП-10", callback_data="bt:top")
    _back(kb)
    kb.adjust(2, 1)
    return kb.as_markup()
