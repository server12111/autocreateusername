"""Проверка ника в соцсетях: YouTube, X (Twitter), TikTok.

Instagram автоматически не проверяется: без входа в аккаунт он отвечает 429 на любые запросы,
а проверка через аккаунты быстро ведёт к их бану — поэтому для него только ссылка.

Статусы: free | taken | reserved (никем не занят, но сеть его не даёт) |
invalid (ник не подходит по правилам сети) | unknown (сеть не ответила).
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass

import aiohttp

from services.http import get_session, proxy, random_headers

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Network:
    code: str
    title: str
    emoji_id: str  # премиум-эмодзи из пака t.me/addemoji/LogosEmoji
    char: str  # обычный эмодзи, если премиум-эмодзи выключены
    url: str  # ссылка на профиль, {name}
    name_re: re.Pattern


TELEGRAM = Network("tg", "Telegram", "5352877806122447940", "✈️", "https://t.me/{name}",
                   re.compile(r"[a-zA-Z](?!.*__)[a-zA-Z0-9_]{3,30}[a-zA-Z0-9]"))
YOUTUBE = Network("yt", "YouTube", "5355235592844095825", "▶️", "https://www.youtube.com/@{name}",
                  re.compile(r"[a-zA-Z0-9_.\-]{3,30}"))
X = Network("x", "X (Twitter)", "5355148941878900494", "✖️", "https://x.com/{name}",
            re.compile(r"[a-zA-Z0-9_]{4,15}"))
TIKTOK = Network("tt", "TikTok", "5353034628263330616", "🎵", "https://www.tiktok.com/@{name}",
                 re.compile(r"[a-zA-Z0-9_.]{2,24}"))
INSTAGRAM = Network("ig", "Instagram", "5355097780228470775", "📸", "https://www.instagram.com/{name}/",
                    re.compile(r"[a-zA-Z0-9_.]{1,30}"))

SOCIAL = {n.code: n for n in (YOUTUBE, X, TIKTOK)}  # проверяются автоматически
ALL = {n.code: n for n in (TELEGRAM, YOUTUBE, X, TIKTOK, INSTAGRAM)}

_TIMEOUT = aiohttp.ClientTimeout(total=10)
_CACHE_TTL = 60  # сек: повторные проверки того же ника не дёргают сеть
_cache: dict[tuple[str, str], tuple[str, float]] = {}
# TikTok начинает не отвечать при пачке параллельных запросов — ему меньше параллельности
_sems = {"yt": asyncio.Semaphore(4), "x": asyncio.Semaphore(4), "tt": asyncio.Semaphore(2)}


def icon(code: str) -> str:
    """Премиум-эмодзи сети для текста (ui_style заменит на обычный, если премиум-эмодзи выключены)."""
    n = ALL[code]
    return f'<tg-emoji emoji-id="{n.emoji_id}">{n.char}</tg-emoji>'


async def _youtube(name: str) -> str:
    async with get_session().get(
        f"https://www.youtube.com/@{name}", headers=random_headers(), proxy=proxy(), timeout=_TIMEOUT,
        allow_redirects=True,
    ) as resp:
        if resp.status == 404:
            return "free"
        return "taken" if resp.status == 200 else "unknown"


async def _x(name: str) -> str:
    # Тот же запрос, что делает форма регистрации X: отвечает именно «можно ли занять ник»
    async with get_session().get(
        "https://api.x.com/i/users/username_available.json", params={"username": name},
        headers=random_headers(), proxy=proxy(), timeout=_TIMEOUT,
    ) as resp:
        if resp.status != 200:
            return "unknown"
        data = await resp.json(content_type=None)
    if data.get("valid") is True:
        return "free"
    if data.get("reason") == "taken":
        return "taken"
    # is_banned_word, contains_banned_word и т. п. — ник никем не занят, но X его не даёт
    return "reserved"


async def _tiktok(name: str) -> str:
    # oEmbed лёгкий (десятки байт): 200 — профиль есть, 400 — нет. 400 бывает и при сбое,
    # поэтому «свободен» подтверждаем по странице профиля (statusCode 10221 = пользователь не найден)
    async with get_session().get(
        "https://www.tiktok.com/oembed", params={"url": f"https://www.tiktok.com/@{name}"},
        headers=random_headers(), proxy=proxy(), timeout=_TIMEOUT,
    ) as resp:
        if resp.status == 200:
            return "taken"
        if resp.status != 400:
            return "unknown"
    async with get_session().get(
        f"https://www.tiktok.com/@{name}", headers=random_headers(), proxy=proxy(),
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        html = await resp.text()
    if '"statusCode":10221' in html:
        return "free"
    if '"statusCode":0' in html:
        return "taken"
    return "unknown"


_CHECKERS = {"yt": _youtube, "x": _x, "tt": _tiktok}


async def check(code: str, name: str, use_cache: bool = True) -> str:
    net = SOCIAL[code]
    if not net.name_re.fullmatch(name):
        return "invalid"
    key = (code, name.lower())
    cached = _cache.get(key)
    if use_cache and cached and time.monotonic() - cached[1] < _CACHE_TTL:
        return cached[0]
    try:
        async with _sems[code]:
            status = await _CHECKERS[code](name)
    except Exception as e:
        log.debug("Соцсети: %s %s — %r", code, name, e)
        status = "unknown"
    if status != "unknown":
        _cache[key] = (status, time.monotonic())
        if len(_cache) > 5000:
            _cache.clear()
    return status


async def check_all(name: str, codes=None, use_cache: bool = True) -> dict[str, str]:
    codes = list(codes or SOCIAL)
    results = await asyncio.gather(*(check(c, name, use_cache) for c in codes))
    return dict(zip(codes, results))


FUNNEL = ("x", "yt", "tt")  # порядок отсева: X строже всех, TikTok — самый капризный, его в конце


async def free_everywhere(names: list[str], codes=None) -> list[str]:
    """Ники, свободные во всех выбранных соцсетях (по умолчанию — во всех).
    Проверяем воронкой: следующая сеть — только для прошедших предыдущую."""
    alive = list(names)
    for code in (c for c in FUNNEL if codes is None or c in codes):
        if not alive:
            break
        results = await asyncio.gather(*(check(code, n) for n in alive))
        alive = [n for n, st in zip(alive, results) if st == "free"]
    return alive


STATUS_MARK = {
    "free": "✅ свободен",
    "taken": "❌ занят",
    "reserved": "🔒 зарезервирован сетью",
    "invalid": "⛔️ не подходит по правилам сети",
    "unknown": "❔ не удалось проверить",
}
