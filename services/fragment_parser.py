import logging
import re

from services.http import get_session, proxy, random_headers

log = logging.getLogger(__name__)

_STATUS_RE = re.compile(r'tm-section-header-status\s+tm-status-(\w+)">([^<]*)<')
_PRICE_RE = re.compile(r'table-cell-value tm-value icon-before icon-ton">([\d,.\s]+)<')


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
        try:
            async with get_session().get(
                cls.BASE_URL + clean, headers=random_headers(), allow_redirects=False, proxy=proxy()
            ) as resp:
                if resp.status in (301, 302, 303, 404):
                    return {"available": True, "status": "free", "price": None}
                if resp.status != 200:
                    return {"available": True, "status": "error", "price": None}
                html = await resp.text()
        except Exception as e:
            log.debug("Fragment error for %s: %s", clean, e)
            return {"available": True, "status": "error", "price": None}

        m = _STATUS_RE.search(html)
        price_m = _PRICE_RE.search(html)
        price = price_m.group(1).strip() if price_m else None
        if not m:
            return {"available": True, "status": "free", "price": None}

        css, text = m.group(1), m.group(2).strip().lower()
        if css == "avail":
            status = "auction" if "auction" in text else "sale"
            return {"available": False, "status": status, "price": price}
        if css == "taken":
            return {"available": False, "status": "taken", "price": price}
        return {"available": False, "status": "sold", "price": price}
