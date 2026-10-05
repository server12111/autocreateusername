import asyncio
import logging
import random
import re
import string
import time
from dataclasses import dataclass

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from services.fragment_parser import FragmentParser
from services.http import get_session, proxy, random_headers
from services.mtproto_pool import BotResolver, MTProtoPool

log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[a-zA-Z](?!.*__)[a-zA-Z0-9_]{3,30}[a-zA-Z0-9]$")

VOWELS = "aeiou"
CONSONANTS = "bcdfghjklmnprstvwxz"
SOFT_CONSONANTS = "bdfgklmnprstvz"
DIGITS = string.digits

# Шаблоны «красивых» ников
PATTERNS_5 = ["CVCVC", "CVCVV", "VCVCV", "CVCCV", "CVVCV", "CCVCV"]
PATTERNS_6 = ["CVCVCV", "CVCCVC", "VCVCVC", "CVCVVC", "CCVCVC", "CVVCVC"]
PREFIXES = ["neo", "zen", "lux", "vex", "kai", "ryo", "nox", "sky", "max", "ion", "evo", "vio", "mio", "lio", "rex"]
ONSETS = ["br", "cr", "dr", "fr", "gr", "kr", "pr", "tr", "bl", "cl", "fl", "gl", "kl", "pl", "sl", "st", "sk", "sp", "sn", "sm", "zr"]
CODAS = ["nd", "nt", "rk", "rt", "ld", "lt", "st", "nk", "mb", "mp", "rn", "rm", "ls", "ns", "rd", "lv", "rv", "xt"]
SUFFIXES = ["ix", "ex", "ox", "io", "ia", "on", "en", "ax", "yx", "us", "ro", "ka", "zo", "ly", "um"]


def normalize(username: str) -> str:
    return username.strip().lstrip("@").split("/")[-1].strip()


def is_valid_username(username: str) -> bool:
    return bool(USERNAME_RE.match(username))


def _from_pattern(pattern: str, nice: bool = False) -> str:
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if nice and pattern[i : i + 2] == "CC":
            out.append(random.choice(ONSETS if i == 0 else CODAS))
            i += 2
            continue
        i += 1
        if ch == "C":
            out.append(random.choice(SOFT_CONSONANTS if random.random() < 0.7 else CONSONANTS))
        elif ch == "V":
            out.append(random.choice(VOWELS))
        elif ch == "D":
            out.append(random.choice(DIGITS))
        elif ch == "*":
            out.append(random.choice(string.ascii_lowercase + DIGITS))
        else:
            out.append(ch.lower())
    return "".join(out)


def generate_nice(length: int) -> str:
    """Читаемый ник заданной длины (чередование гласных/согласных + морфемы)."""
    roll = random.random()
    if roll < 0.25:
        prefix = random.choice(PREFIXES)
        tail = length - len(prefix)
        if tail > 0:
            return prefix + _from_pattern(("VC" * 3)[:tail] if prefix[-1] not in VOWELS else ("CV" * 3)[:tail], True)
    if roll < 0.45:
        suffix = random.choice(SUFFIXES)
        head = length - len(suffix)
        if head > 0:
            return _from_pattern(("CV" * 3)[:head], True) + suffix
    return _from_pattern(random.choice(PATTERNS_5 if length == 5 else PATTERNS_6), True)


def validate_mask(mask: str) -> str | None:
    """Возвращает текст ошибки или None. C, V, D и * — подстановки, остальные символы буквальные."""
    if not 5 <= len(mask) <= 32:
        return "Длина маски должна быть от 5 до 32 символов."
    if not re.fullmatch(r"[A-Za-z0-9_*]+", mask):
        return "Маска может содержать только латиницу, цифры, «_» и «*»."
    if not re.fullmatch(r"[a-zCV*]", mask[0]):
        return "Юзернейм должен начинаться с буквы (C, V, * или конкретная буква)."
    if mask[-1] == "_":
        return "Юзернейм не может заканчиваться на «_»."
    if "__" in mask:
        return "Два подчёркивания подряд недопустимы."
    if not any(ch in "CVD*" for ch in mask):
        return "В маске нет подстановок (C, V, D или *) — используйте «Ловушку» для конкретного ника."
    return None


def generate_from_mask(mask: str) -> str:
    while True:
        name = _from_pattern(mask)
        if is_valid_username(name):
            return name


class CheckerUnavailable(Exception):
    """t.me / Fragment / Telegram подряд не отвечают — продолжать поиск бессмысленно."""


@dataclass
class CheckResult:
    username: str
    status: str  # free | taken | fragment_auction | fragment_sale | fragment_sold | reserved | invalid | unknown
    tg_free: bool
    fragment_free: bool
    fragment_price: str | None = None
    source: str = "mtproto"  # mtproto | bot | web

    @property
    def is_free(self) -> bool:
        return self.status == "free"


class UsernameChecker:
    """Двойная проверка юзернейма: Telegram (MTProto или резервный web/Bot API) + Fragment."""

    MAX_UNKNOWN_STREAK = 18  # столько проверок подряд без ответа — считаем, что сервисы недоступны

    def __init__(self, pool: MTProtoPool, concurrency: int = 8, resolver: BotResolver | None = None):
        self.pool = pool
        self.resolver = resolver
        self.bot: Bot | None = None
        self.sem = asyncio.Semaphore(concurrency)

    async def _tme_exists(self, username: str) -> bool | None:
        """Публичная страница t.me: есть заголовок -> ник занят. None — не удалось проверить."""
        try:
            async with get_session().get(
                f"https://t.me/{username}", headers=random_headers(), proxy=proxy(),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
            return "tgme_page_title" in html
        except Exception:
            return None

    async def _bot_resolve(self, username: str) -> bool:
        if not self.bot:
            return False
        try:
            await self.bot.get_chat(f"@{username}")
            return True
        except TelegramBadRequest:
            return False
        except Exception:
            return False

    async def check(self, username: str) -> CheckResult:
        username = normalize(username)
        if not is_valid_username(username):
            return CheckResult(username, "invalid", False, False)

        async with self.sem:
            # 1) Быстрый фильтр по t.me — экономит лимиты MTProto
            tme = await self._tme_exists(username)
            if tme:
                return CheckResult(username, "taken", False, True, source="web")

            # 2) Fragment
            frag = await FragmentParser.check_username(username)
            if frag["status"] in ("auction", "sale", "sold", "taken"):
                status = {
                    "auction": "fragment_auction",
                    "sale": "fragment_sale",
                    "sold": "fragment_sold",
                    "taken": "taken",
                }[frag["status"]]
                return CheckResult(username, status, False, False, frag["price"])

            # 3) MTProto под токеном бота: видит пользователей и зарезервированные ники
            resolved = await self.resolver.resolve(username) if self.resolver else "unavailable"
            if resolved == "occupied":
                return CheckResult(username, "taken", False, True, source="bot")
            if resolved == "reserved":
                return CheckResult(username, "reserved", False, True, source="bot")

            # 4) Финальная проверка через аккаунты пула (account.checkUsername)
            mt = await self.pool.check_username(username)
            st = mt["status"]
            if st == "free":
                return CheckResult(username, "free", True, True)
            if st == "occupied":
                return CheckResult(username, "taken", False, True)
            if st == "fragment_only":
                return CheckResult(username, "fragment_sold", False, False)
            if st == "invalid":
                return CheckResult(username, "invalid", False, True)

            # 5) Аккаунтов нет / все во флуде
            if resolved == "free" and frag["status"] != "error":
                return CheckResult(username, "free", True, True, source="bot")
            if self.resolver and self.resolver.enabled:
                # Бот-проверка есть, но сейчас недоступна — не выдаём непроверенный ник за свободный
                return CheckResult(username, "unknown", False, False, source="bot")
            if await self._bot_resolve(username):
                return CheckResult(username, "taken", False, True, source="web")
            # Без MTProto «свободен» можно сказать, только если t.me и Fragment ответили
            if tme is None or frag["status"] == "error":
                return CheckResult(username, "unknown", False, False, source="web")
            return CheckResult(username, "free", True, True, source="web")

    async def find_free(
        self,
        generator,
        batch: int = 6,
        exclude: set[str] | None = None,
        max_seconds: float = 600,
    ) -> tuple[CheckResult | None, int]:
        """Проверяет кандидатов пачками, пока не найдёт свободный.

        Останавливается раньше, только если варианты маски закончились или прошло max_seconds
        (страховка от бесконечного поиска). Возвращает (результат, проверено).
        """
        exclude = set(exclude or ())
        checked = 0
        unknown_streak = 0  # подряд проверок без ответа от сервисов
        deadline = time.monotonic() + max_seconds
        while time.monotonic() < deadline:
            names: list[str] = []
            tries = 0
            while len(names) < batch and tries < 500:
                tries += 1
                n = generator()
                if is_valid_username(n) and n not in exclude:
                    exclude.add(n)
                    names.append(n)
            if not names:
                break
            results = await asyncio.gather(*(self.check(n) for n in names))
            checked += len(results)
            for r in results:
                if r.is_free:
                    return r, checked
                unknown_streak = unknown_streak + 1 if r.status == "unknown" else 0
            if unknown_streak >= self.MAX_UNKNOWN_STREAK:
                log.warning("Поиск остановлен: %d проверок подряд без ответа от сервисов", unknown_streak)
                raise CheckerUnavailable()
        return None, checked


STATUS_TEXT = {
    "free": "✅ Свободен",
    "taken": "❌ Занят (пользователь, канал, группа или бот)",
    "fragment_auction": "🔨 Торги на Fragment",
    "fragment_sale": "💰 Продаётся на Fragment",
    "fragment_sold": "💎 Продан / зарезервирован на Fragment",
    "invalid": "⛔️ Недопустимый юзернейм",
    "reserved": "🔒 Зарезервирован Telegram — занять нельзя",
    "unknown": "❔ Не удалось проверить",
}
