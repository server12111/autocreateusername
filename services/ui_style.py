"""Оформление исходящих сообщений: премиум-эмодзи в текстах/кнопках и цветные кнопки.

Работает как request-middleware сессии бота, поэтому применяется ко всем сообщениям
без правок в хэндлерах. Настройки хранятся в таблице settings:
  premium_emoji  — JSON {"🔍": "<custom_emoji_id>", ...}
  premium_emoji_enabled, button_colors_enabled — "1"/"0"
"""

import json
import logging
import os
import re
import time

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageReplyMarkup, EditMessageText, SendMessage, SendPhoto, TelegramMethod
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import crud
from database.base import session_maker

log = logging.getLogger(__name__)

# Куски HTML, внутри которых эмодзи не трогаем: уже обёрнутые эмодзи, <code>/<pre> и сами теги
_PROTECTED_RE = re.compile(r"<tg-emoji[^>]*>.*?</tg-emoji>|<(code|pre)\b[^>]*>.*?</\1>|<[^>]+>", re.S)

VS16 = "️"

# Эмодзи, которые используются в интерфейсе (для подсказки в админке)
UI_EMOJI = [
    "🔍", "💎", "🔤", "🎯", "🪤", "🛒", "👤", "👥", "⚔", "🆘", "✅", "❌", "🎉", "🚀", "🔄", "📁",
    "🔙", "⭐", "📦", "🎁", "🎟", "ℹ", "📢", "⏳", "⚡", "🛡", "📊", "🔥", "💵", "🏆", "👑", "⏭",
    "🗑", "🚨", "🧾", "💳", "🆔", "👋", "🤖", "⏱", "😔", "⚙", "📜", "🔗", "🥉", "🥈", "🥇", "🏠",
]

# ───────────────────────── цвета кнопок ─────────────────────────

SUCCESS_DATA = {"check_op_sub", "s:bonus", "s:5", "s:6", "s:m", "trap:add", "adm:sp:add", "adm:pr:new",
                "adm:bc:go", "adm:acc:phone", "adm:acc:upload", "shop:premium"}
SUCCESS_PREFIX = ("buy:", "pay:", "bt:v:", "cap:")
PRIMARY_DATA = {"menu:search", "menu:shop", "menu:profile", "menu:ref", "menu:battle", "shop:packs",
                "s:mask", "s:trap", "prof:promo", "prof:finds", "prof:info", "bt:top"}
DANGER_MARKS = ("❌", "🗑", "⛔", "🔴")
SUCCESS_URL_MARKS = ("🚀", "🦋")
PRIMARY_URL_MARKS = ("📢", "🆘")


TG_EMOJI_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.S)


def strip_tg_emoji(text: str | None) -> str | None:
    return TG_EMOJI_RE.sub(lambda m: m.group(1), text) if text else text


def _strip_vs(s: str) -> str:
    return s.replace(VS16, "")


def button_style(btn: InlineKeyboardButton) -> str | None:
    text = btn.text.lstrip()
    if text.startswith(DANGER_MARKS):
        return "danger"
    if text.startswith("🔙"):
        return None
    data = btn.callback_data or ""
    if btn.url:
        if text.startswith(SUCCESS_URL_MARKS):
            return "success"
        if text.startswith(PRIMARY_URL_MARKS):
            return "primary"
        return None
    if btn.pay or data in SUCCESS_DATA or data.startswith(SUCCESS_PREFIX):
        return "success"
    if data in PRIMARY_DATA or data.startswith("adm:") and not data.startswith("adm:home"):
        return "primary"
    return None


# ───────────────────────── премиум-эмодзи ─────────────────────────


class EmojiMap:
    def __init__(self, mapping: dict[str, str]):
        # ключи без VS16, чтобы «⚡» и «⚡️» совпадали
        self.mapping = {_strip_vs(k): v for k, v in mapping.items() if v.isdigit()}
        keys = sorted(self.mapping, key=len, reverse=True)
        self.regex = re.compile("|".join(re.escape(k) + VS16 + "?" for k in keys)) if keys else None

    def wrap_text(self, text: str) -> str:
        """Заменяет эмодзи на <tg-emoji>, не трогая содержимое HTML-тегов и уже обёрнутые эмодзи."""
        if not self.regex or not text:
            return text
        out, pos = [], 0
        for m in _PROTECTED_RE.finditer(text):
            out.append(self._sub(text[pos : m.start()]))
            out.append(m.group(0))
            pos = m.end()
        out.append(self._sub(text[pos:]))
        return "".join(out)

    def _sub(self, chunk: str) -> str:
        return self.regex.sub(
            lambda m: f'<tg-emoji emoji-id="{self.mapping[_strip_vs(m.group(0))]}">{m.group(0)}</tg-emoji>', chunk
        )

    def leading(self, text: str) -> tuple[str | None, str]:
        """Если кнопка начинается с известного эмодзи — вернуть его id и текст без эмодзи."""
        if not self.regex:
            return None, text
        m = self.regex.match(text.lstrip())
        if not m:
            return None, text
        rest = text.lstrip()[m.end():].lstrip()
        return (self.mapping[_strip_vs(m.group(0))], rest) if rest else (None, text)


_CONFIG_KEYS = ("premium_emoji_enabled", "button_colors_enabled", "premium_emoji")


async def load_config() -> tuple[EmojiMap | None, bool]:
    cache = crud._settings_cache
    if not all(k in cache for k in _CONFIG_KEYS):
        # Настройки кэшируются в памяти после первого чтения — БД открываем только один раз
        async with session_maker() as s:
            for k in _CONFIG_KEYS:
                await crud.get_setting(s, k)
    emoji_on = cache.get("premium_emoji_enabled") == "1"
    colors_on = cache.get("button_colors_enabled") == "1"
    return (_build_map(cache.get("premium_emoji", "")) if emoji_on else None), colors_on


def _load_bundled() -> dict[str, str]:
    """Встроенный пак: официальные анимированные эмодзи Telegram (t.me/addemoji/RestrictedEmoji)."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "emoji_pack.json"), encoding="utf-8") as f:
            return json.load(f)["map"]
    except (OSError, ValueError, KeyError):
        return {}


BUNDLED_EMOJI = _load_bundled()
_map_cache: tuple[str, EmojiMap] | None = None


def _build_map(raw: str) -> EmojiMap | None:
    """Встроенный пак + эмодзи, добавленные админом (они важнее). Результат кэшируется."""
    global _map_cache
    if _map_cache and _map_cache[0] == raw:
        return _map_cache[1]
    try:
        custom = json.loads(raw or "{}")
    except ValueError:
        custom = {}
    emap = EmojiMap({**BUNDLED_EMOJI, **custom})
    _map_cache = (raw, emap)
    return emap


def style_markup(markup, emap: EmojiMap | None, colors: bool):
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for btn in row:
            update = {}
            if colors and btn.style is None:
                st = button_style(btn)
                if st:
                    update["style"] = st
            if btn.icon_custom_emoji_id is not None:
                # Иконка задана в клавиатуре, а текст начинается с обычного эмодзи-запаски:
                # с премиум-эмодзи убираем запаску, без них — иконку
                head, _, rest = btn.text.partition(" ")
                if emap:
                    if rest and not head.isalnum():
                        update["text"] = rest
                else:
                    update["icon_custom_emoji_id"] = None
            elif emap:
                emoji_id, rest = emap.leading(btn.text)
                if emoji_id:
                    update["icon_custom_emoji_id"] = emoji_id
                    update["text"] = rest
            new_row.append(btn.model_copy(update=update) if update else btn)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


class UiStyleMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request: NextRequestMiddlewareType, bot: Bot, method: TelegramMethod):
        if not isinstance(method, (SendMessage, EditMessageText, EditMessageReplyMarkup, SendPhoto)):
            return await make_request(bot, method)

        global _emoji_off_until
        emap, colors = await load_config()
        if emap and time.time() < _emoji_off_until:
            emap = None
        try:
            return await make_request(bot, _styled(method, emap, colors))
        except TelegramBadRequest as e:
            if not emap or any(s in str(e).lower() for s in _NOT_STYLE_ERRORS):
                raise
            # Например, у владельца бота нет Telegram Premium: отключаем премиум-эмодзи на час,
            # цветные кнопки оставляем
            _emoji_off_until = time.time() + 3600
            log.warning("Премиум-эмодзи отклонены Telegram (%s) — отключены на 1 час", e)
            return await make_request(bot, _styled(method, None, colors))


# Ошибки, не связанные с оформлением, — повторять запрос без него бессмысленно
_NOT_STYLE_ERRORS = ("not modified", "not found", "can't be edited", "message to edit", "chat not found")
_emoji_off_until = 0.0


def _styled(method: TelegramMethod, emap: EmojiMap | None, colors: bool) -> TelegramMethod:
    update = {}
    markup = getattr(method, "reply_markup", None)
    if markup is not None:
        update["reply_markup"] = style_markup(markup, emap, colors)
    if isinstance(method, (SendMessage, EditMessageText)):
        update["text"] = emap.wrap_text(method.text) if emap else strip_tg_emoji(method.text)
    if isinstance(method, SendPhoto) and method.caption:
        update["caption"] = emap.wrap_text(method.caption) if emap else strip_tg_emoji(method.caption)
    return method.model_copy(update=update)
