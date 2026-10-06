from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import PREMIUM_PLANS, SEARCH_PACKS
from services import social_checker
from services.crypto_pay import PROVIDERS, provider_name


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
    kb.button(text="🌐 Ник в соцсетях", callback_data="menu:social")
    kb.button(text="🛒 Магазин", callback_data="menu:shop")
    kb.button(text="👤 Профиль", callback_data="menu:profile")
    kb.button(text="👥 Рефералы", callback_data="menu:ref")
    if battle_enabled:
        kb.button(text="⚔️ Битва Никнеймов", callback_data="menu:battle")
    kb.button(text="🆘 Поддержка", url=support_url or "https://t.me/")
    kb.adjust(2)
    return kb.as_markup()


def sponsor_bonus_kb(channels) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for ch in channels:
        kb.button(text=f"📢 {ch.title}"[:64], url=ch.url)
    kb.button(text="✅ Проверить подписку", callback_data="check_op_sub")
    kb.button(text="💎 Купить Premium", callback_data="shop:premium")
    kb.button(text="🔙 Назад в поиск", callback_data="menu:search")
    kb.adjust(1)
    return kb.as_markup()


def search_kb(sponsor_bonus: int = 0) -> InlineKeyboardMarkup:
    """sponsor_bonus > 0 — показать кнопку бонуса за подписку на спонсоров."""
    kb = InlineKeyboardBuilder()
    kb.button(text="💎 5 букв (Редкие)", callback_data="s:5")
    kb.button(text="🔤 6 букв", callback_data="s:6")
    kb.button(text="✍️ Поиск по слову", callback_data="s:word")
    kb.button(text="🎯 Поиск по маске", callback_data="s:mask")
    kb.button(text="🪤 Ловушка на ник (Снайпер)", callback_data="s:trap")
    if sponsor_bonus:
        kb.button(text=f"🎁 +{sponsor_bonus} поиска за подписку", callback_data="s:bonus")
    _back(kb)
    kb.adjust(2, 2, 1, 1, 1)
    return kb.as_markup()


def word_found_kb(word: str, has_more: bool, can_trap: bool, can_save: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if has_more:
        kb.button(text="🔄 Ещё варианты", callback_data="s:wmore")
    if can_save:
        kb.button(text="📁 Сохранить все", callback_data="s:wsave")
    if can_trap:
        kb.button(text=f"🪤 Ловушка на @{word}", callback_data="s:wtrap")
    kb.button(text="✍️ Другое слово", callback_data="s:word")
    _back(kb, "menu:search", "🔙 Назад в поиск")
    kb.adjust(2, 1, 1, 1)
    return kb.as_markup()


def found_kb(username: str, mode: str, search_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Искать ещё", callback_data=f"s:{mode}")
    kb.button(text="📁 В мои находки", callback_data=f"s:save:{search_id}")
    _back(kb, "menu:search", "🔙 Назад в поиск")
    kb.adjust(2, 1)
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


def network_button(code: str, text: str, **kwargs) -> InlineKeyboardButton:
    """Кнопка с премиум-логотипом соцсети (обычный эмодзи в начале — запасной вариант, см. ui_style)."""
    net = social_checker.ALL[code]
    return InlineKeyboardButton(text=f"{net.char} {text}", icon_custom_emoji_id=net.emoji_id, **kwargs)


def trap_networks_kb(catchable: list[str], selected: list[str]) -> InlineKeyboardMarkup:
    """Галочки «где ловить ник» для ловушки."""
    kb = InlineKeyboardBuilder()
    for code in catchable:
        mark = "✅" if code in selected else "⬜️"
        kb.add(network_button(code, f"{social_checker.ALL[code].title} {mark}", callback_data=f"trap:net:{code}"))
    kb.button(text="🪤 Поставить ловушку", callback_data="trap:ok")
    kb.button(text="❌ Отмена", callback_data="s:trap")
    kb.adjust(1)
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


def _plan_buttons(kb: InlineKeyboardBuilder, prices: dict[str, int] | None, pct: int) -> None:
    """prices — цены со скидкой по ключам тарифов (None — обычные цены)."""
    for key, (days, price) in PREMIUM_PLANS.items():
        price = prices[key] if prices else price
        mark = f" (−{pct}%)" if pct else ""
        kb.button(text=f"💎 {days} дн. — {price} ⭐️{mark}", callback_data=f"buy:{key}")


def premium_plans_kb(prices: dict[str, int] | None = None, pct: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _plan_buttons(kb, prices, pct)
    _back(kb, "menu:shop", "🔙 В магазин")
    kb.adjust(1 if pct else 2, 1 if pct else 2, 1, 1, 1)
    return kb.as_markup()


def renew_kb(prices: dict[str, int] | None = None, pct: int = 0) -> InlineKeyboardMarkup:
    """Продление Premium из напоминания и из сообщения об окончании."""
    kb = InlineKeyboardBuilder()
    _plan_buttons(kb, prices, pct)
    _back(kb, "menu:main", "🏠 Главное меню")
    kb.adjust(1 if pct else 2, 1 if pct else 2, 1, 1, 1)
    return kb.as_markup()


def packs_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, (count, price) in SEARCH_PACKS.items():
        kb.button(text=f"🔍 {count} поисков — {price} ⭐️", callback_data=f"buy:{key}")
    _back(kb, "menu:shop", "🔙 В магазин")
    kb.adjust(1)
    return kb.as_markup()


def _provider_button(provider: str, text: str, **kwargs) -> InlineKeyboardButton:
    """Кнопка с премиум-эмодзи платёжки. Обычный эмодзи в начале текста — запасной вариант,
    если премиум-эмодзи выключены (ui_style уберёт лишнее)."""
    _, emoji_id, char = PROVIDERS[provider]
    return InlineKeyboardButton(text=f"{char} {text}", icon_custom_emoji_id=emoji_id, **kwargs)


def pay_method_kb(key: str, stars: int, usd: str | None, providers: list[str], back: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"⭐️ Telegram Stars — {stars} ⭐️", callback_data=f"pay:st:{key}")
    if usd:
        for p in providers:
            kb.add(_provider_button(p, f"{provider_name(p)} — ${usd}", callback_data=f"pay:{p}:{key}"))
    _back(kb, back, "🔙 Назад")
    kb.adjust(1)
    return kb.as_markup()


def crypto_invoice_kb(provider: str, pay_url: str, inv_id: int, back: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.add(_provider_button(provider, f"Оплатить в {provider_name(provider)}", url=pay_url))
    kb.button(text="✅ Я оплатил — проверить", callback_data=f"pay:chk:{inv_id}")
    _back(kb, back, "🔙 Назад")
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
