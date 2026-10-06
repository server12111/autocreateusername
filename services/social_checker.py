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
            re.compile(r"[a-zA-Z0-9_]{5,15}"))  # 4-символьные X уже не выдаёт (invalid_username)
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
        if resp.status == 200:
            return "taken"
        _fail("yt", f"HTTP {resp.status}")
        return "unknown"


async def _x(name: str) -> str:
    # Тот же запрос, что делает форма регистрации X: отвечает именно «можно ли занять ник»
    async with get_session().get(
        "https://api.x.com/i/users/username_available.json", params={"username": name},
        headers=random_headers(), proxy=proxy(), timeout=_TIMEOUT,
    ) as resp:
        if resp.status != 200:
            _fail("x", f"HTTP {resp.status}")
            return "unknown"
        data = await resp.json(content_type=None)
    if data.get("valid") is True:
        return "free"
    reason = data.get("reason")
    if reason == "taken":
        return "taken"
    if reason == "invalid_username":  # например, короче 5 символов — X такие больше не выдаёт
        return "invalid"
    # is_banned_word, contains_banned_word и т. п. — аккаунта может и не быть, но X этот ник не даёт
    # (заблокированные слова, ники удалённых и забаненных аккаунтов)
    return "reserved"


async def _tiktok(name: str) -> str:
    # oEmbed лёгкий (десятки байт): 200 — профиль есть, 400 — нет. 400 бывает и при сбое, а другие
    # коды — при блокировке, поэтому во всех сомнительных случаях смотрим страницу профиля
    # (statusCode 10221 — пользователь не найден, 0 — найден)
    try:
        async with get_session().get(
            "https://www.tiktok.com/oembed", params={"url": f"https://www.tiktok.com/@{name}"},
            headers=random_headers(), proxy=proxy(), timeout=_TIMEOUT,
        ) as resp:
            if resp.status == 200:
                return "taken"
            if resp.status != 400:
                _fail("tt", f"oembed HTTP {resp.status}")
    except Exception as e:
        _fail("tt", f"oembed {type(e).__name__}")
    async with get_session().get(
        f"https://www.tiktok.com/@{name}", headers=random_headers(), proxy=proxy(),
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        html = await resp.text()
        page_status = resp.status
    if '"statusCode":10221' in html:
        return "free"
    if '"statusCode":0' in html:
        return "taken"
    _fail("tt", f"страница HTTP {page_status}, {len(html)} байт, без данных профиля (капча или блокировка)")
    return "unknown"


# Последняя причина, по которой сеть не ответила, — для диагностики в админке
last_error: dict[str, str] = {}


def _fail(code: str, reason: str) -> None:
    if last_error.get(code) != reason:
        log.warning("Соцсети: %s не ответил — %s", ALL[code].title, reason)
    last_error[code] = reason


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
        # Таймаут, обрыв соединения, блокировка провайдером — причину видно в диагностике админки
        _fail(code, f"{type(e).__name__}: {e}"[:200] if str(e) else type(e).__name__)
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
    "reserved": "🔒 недоступен — сеть не даёт его занять",
    "invalid": "⛔️ не подходит по правилам сети",
    "unknown": "❔ не удалось проверить",
}
