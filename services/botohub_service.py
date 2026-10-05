import logging
from typing import Any

import aiohttp

from services.http import get_session

log = logging.getLogger(__name__)


class BotohubService:
    """Интеграция с BotoHub (https://botohub.me/integration)."""

    BASE_URL = "https://botohub.me"
    MAX_OP = 5  # сколько спонсоров показывать за раз

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def request_tasks(self, chat_id: int) -> dict[str, Any]:
        """Сырой ответ /get-tasks-extended (используется и для теста подключения в админке)."""
        payload = {"chat_id": chat_id, "max_op": self.MAX_OP, "only_has_check": True}
        async with get_session().post(
            f"{self.BASE_URL}/get-tasks-extended",
            json=payload,
            headers={"Auth": self.api_key, "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {"error": (await resp.text())[:300]}
            if not isinstance(data, dict):
                data = {"error": str(data)[:300]}
            data["_http_status"] = resp.status
            return data

    async def get_unsubscribed(self, chat_id: int) -> list[str]:
        """Ссылки спонсоров, на которые пользователь ещё НЕ подписан."""
        if not self.api_key:
            return []
        try:
            data = await self.request_tasks(chat_id)
        except Exception as e:
            log.warning("BotoHub недоступен: %s", e)
            return []
        if data.get("_http_status") != 200 or "error" in data:
            log.warning("BotoHub: ошибка ответа %s", data)
            return []
        if data.get("skip") or data.get("completed"):
            return []
        links = []
        for task in data.get("tasks") or []:
            if isinstance(task, str):
                links.append(task)
            elif isinstance(task, dict) and task.get("url") and not task.get("completed"):
                links.append(task["url"])
        return links
