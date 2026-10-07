import asyncio
import logging
import re
import time

import aiohttp

from services.http import get_session, proxy, random_headers

log = logging.getLogger(__name__)

_STATUS_RE = re.compile(r'tm-section-header-status\s+tm-status-(\w+)">([^<]*)<')
_PRICE_RE = re.compile(r'table-cell-value tm-value icon-before icon-ton">([\d,.\s]+)<')


# Ограничиваем параллельные запросы к Fragment, чтобы он не начал отдавать заглушки
_sem = asyncio.Semaphore(6)

# После ~150 быстрых запросов Fragment на полминуты-минуту отвечает редиректом на главную.
# Пока лимит не прошёл, не шлём запросы: каждый новый только продлевает блокировку
LIMIT_PAUSE = 30
_limited_until = 0.0


class FragmentParser:
    BASE_URL = "https://fragment.com/username/"

    @classmethod
    async def check_username(cls, username: str) -> dict:
        """
        Проверяет юзернейм на Fragment.
        status:
          free    — не выставлен на Fragment (редирект на поиск / 404)
          auction — идут торги (price = текущая/минимальная ставка в TON)
          sale    — продаётся по фиксированной цене
          sold    — продан на Fragment (не привязан или ждёт привязки)
          taken   — продан и привязан к аккаунту
          error   — не удалось проверить
        """
        clean = username.lstrip("@").lower()
        result = {"available": False, "status": "error", "price": None}
        if time.monotonic() < _limited_until:
            return result
        for attempt in range(2):
            result = await cls._fetch(clean)
            if result["status"] != "error" or time.monotonic() < _limited_until:
                return result
            await asyncio.sleep(0.5 * (attempt + 1))
        log.warning("Fragment: не удалось проверить @%s", clean)
        return result

    @classmethod
    async def _fetch(cls, clean: str) -> dict:
        global _limited_until
        error = {"available": False, "status": "error", "price": None}
        try:
            async with _sem, get_session().get(
                cls.BASE_URL + clean, headers=random_headers(), allow_redirects=False, proxy=proxy(),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status in (301, 302, 303):
                    # Ника нет на Fragment — редирект на поиск. Любой другой редирект — не доверяем
                    location = resp.headers.get("Location", "")
                    if "query=" in location:
                        return {"available": True, "status": "free", "price": None}
                    if location == "/" and time.monotonic() >= _limited_until:
                        _limited_until = time.monotonic() + LIMIT_PAUSE
                        log.warning("Fragment: ограничение частоты запросов — пауза %d сек", LIMIT_PAUSE)
                    return error
                if resp.status == 404:
                    return {"available": True, "status": "free", "price": None}
                if resp.status != 200:
                    return error
                html = await resp.text()
        except Exception as e:
            log.debug("Fragment error for %s: %s", clean, e)
            return error

        m = _STATUS_RE.search(html)
        price_m = _PRICE_RE.search(html)
        price = price_m.group(1).strip() if price_m else None
        if not m:
            # Страница без статуса (заглушка, ограничение частоты) — считать ник свободным нельзя
            return error

        css, text = m.group(1), m.group(2).strip().lower()
        if css == "avail":
            status = "auction" if "auction" in text else "sale"
            return {"available": False, "status": status, "price": price}
        if css == "taken":
            return {"available": False, "status": "taken", "price": price}
        return {"available": False, "status": "sold", "price": price}
