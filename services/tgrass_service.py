import logging
from typing import Any

import aiohttp

from services.http import get_session

log = logging.getLogger(__name__)


class TgrassService:
    """Интеграция с Tgrass (https://tgrass.space/integration)."""

    BASE_URL = "https://tgrass.space"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _headers(self) -> dict:
        return {"Auth": self.api_key, "Content-Type": "application/json"}

    async def request_offers(
        self,
        tg_user_id: int,
        tg_login: str | None = None,
        lang: str = "ru",
        is_premium: bool = False,
    ) -> dict[str, Any]:
        """Сырой ответ /offers (используется и для теста подключения в админке)."""
        payload = {
            "tg_user_id": tg_user_id,
            "tg_login": tg_login or "",
            "lang": lang or "ru",
            "is_premium": bool(is_premium),
        }
        async with get_session().post(
            f"{self.BASE_URL}/offers",
            json=payload,
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {"status": "http_error", "text": (await resp.text())[:300]}
            data["_http_status"] = resp.status
            return data

    async def get_offers(
        self,
        tg_user_id: int,
        tg_login: str | None = None,
        lang: str = "ru",
        is_premium: bool = False,
    ) -> list[dict[str, Any]]:
        """Возвращает офферы, на которые пользователь ещё НЕ подписан."""
        if not self.api_key:
            return []
        try:
            data = await self.request_offers(tg_user_id, tg_login, lang, is_premium)
        except Exception as e:
            log.warning("Tgrass недоступен: %s", e)
            return []
        if data.get("status") not in ("ok", "not_ok"):
            return []
        return [o for o in data.get("offers") or [] if not o.get("subscribed", False) and o.get("link")]

    async def reset_offers(self, tg_user_id: int) -> None:
        if not self.api_key:
            return
        try:
            async with get_session().post(
                f"{self.BASE_URL}/reset_offers", json={"tg_user_id": tg_user_id}, headers=self._headers()
            ):
                pass
        except Exception:
            pass
