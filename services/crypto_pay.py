"""Оплата в долларах через Crypto Pay API (@CryptoBot) и xRocket Pay API.

CryptoBot выставляет счёт в фиатных USD (покупатель платит любой криптой),
xRocket фиат не принимает — счёт выставляется в USDT (1 USDT ≈ 1 $).
"""

import logging
from dataclasses import dataclass

import aiohttp

from config import settings
from services.http import get_session, proxy

log = logging.getLogger(__name__)

CRYPTOBOT_API = "https://pay.crypt.bot/api/"
XROCKET_API = "https://pay.api.xrocket.exchange/api/v1/"

INVOICE_TTL = 3600  # срок жизни счёта, сек
_TIMEOUT = aiohttp.ClientTimeout(total=15)

PROVIDERS = {
    "cb": ("CryptoBot", "5361914370068613491", "🦋"),
    "xr": ("xRocket", "5341788140434637169", "🚀"),
}


def provider_name(code: str) -> str:
    return PROVIDERS[code][0]


def provider_emoji_html(code: str) -> str:
    _, emoji_id, char = PROVIDERS[code]
    return f'<tg-emoji emoji-id="{emoji_id}">{char}</tg-emoji>'


def enabled_providers() -> list[str]:
    tokens = {"cb": settings.CRYPTOBOT_TOKEN, "xr": settings.XROCKET_TOKEN}
    return [code for code in PROVIDERS if tokens[code]]


class CryptoPayError(Exception):
    pass


@dataclass
class CreatedInvoice:
    invoice_id: str
    pay_url: str


# ───────────────────────── CryptoBot ─────────────────────────


async def _cryptobot(method: str, params: dict) -> dict | list:
    async with get_session().post(
        CRYPTOBOT_API + method,
        json=params,
        headers={"Crypto-Pay-API-Token": settings.CRYPTOBOT_TOKEN},
        proxy=proxy(),
        timeout=_TIMEOUT,
    ) as resp:
        data = await resp.json(content_type=None)
    if not data.get("ok"):
        raise CryptoPayError(f"CryptoBot {method}: {data.get('error')}")
    return data["result"]


async def _cryptobot_create(amount: str, description: str, payload: str) -> CreatedInvoice:
    res = await _cryptobot(
        "createInvoice",
        {
            "currency_type": "fiat",
            "fiat": "USD",
            "amount": amount,
            "description": description[:1024],
            "payload": payload,
            "expires_in": INVOICE_TTL,
            "allow_comments": False,
        },
    )
    return CreatedInvoice(str(res["invoice_id"]), res.get("bot_invoice_url") or res["pay_url"])


async def _cryptobot_statuses(ids: list[str]) -> dict[str, str]:
    res = await _cryptobot("getInvoices", {"invoice_ids": ",".join(ids), "count": len(ids)})
    return {str(i["invoice_id"]): i["status"] for i in res.get("items", [])}


# ───────────────────────── xRocket ─────────────────────────


async def _xrocket(method: str, path: str, **kwargs) -> dict:
    async with get_session().request(
        method,
        XROCKET_API + path,
        headers={"Authorization": f"Bearer {settings.XROCKET_TOKEN}"},
        proxy=proxy(),
        timeout=_TIMEOUT,
        **kwargs,
    ) as resp:
        data = await resp.json(content_type=None)
        if resp.status >= 400:
            raise CryptoPayError(f"xRocket {path}: {resp.status} {data.get('detail') or data.get('title')}")
    return data


async def _xrocket_create(amount: str, description: str, payload: str) -> CreatedInvoice:
    res = await _xrocket(
        "POST",
        "invoices",
        json={
            "priceAmount": amount,
            "priceCurrency": "USDT",
            "payCurrencies": ["USDT"],
            "numPayments": 1,
            "description": description[:1000],
            "clientInvoiceId": payload,
            "expiresIn": INVOICE_TTL * 1000,
        },
    )
    return CreatedInvoice(str(res["id"]), res["links"]["telegramBotLink"])


async def _xrocket_statuses(ids: list[str]) -> dict[str, str]:
    # У каждого метода свой лимит 20 запросов/мин: одиночные проверки по кнопке идут через
    # /invoice, чтобы не отнимать лимит у фоновой проверки списком
    if len(ids) == 1:
        res = await _xrocket("GET", "invoice", params={"invoiceId": ids[0]})
        return {str(res["id"]): res["status"]}
    res = await _xrocket("GET", "invoices", params=[("ids[]", i) for i in ids] + [("limit", str(len(ids)))])
    return {str(i["id"]): i["status"] for i in res.get("items", [])}


# ───────────────────────── общий интерфейс ─────────────────────────


async def create_invoice(provider: str, amount: str, description: str, payload: str) -> CreatedInvoice:
    """payload — уникальная строка счёта (xRocket требует уникальный clientInvoiceId)."""
    try:
        if provider == "cb":
            return await _cryptobot_create(amount, description, payload)
        return await _xrocket_create(amount, description, payload)
    except CryptoPayError:
        raise
    except Exception as e:  # сеть, таймаут, неожиданный ответ
        raise CryptoPayError(f"{provider_name(provider)}: {e!r}") from e


async def get_statuses(provider: str, ids: list[str]) -> dict[str, str]:
    """invoice_id -> статус (active | paid | expired | cancelled ...). Пустой dict при ошибке."""
    if not ids:
        return {}
    try:
        if provider == "cb":
            return await _cryptobot_statuses(ids)
        return await _xrocket_statuses(ids)
    except Exception as e:
        log.warning("Не удалось получить статусы счетов %s: %r", provider_name(provider), e)
        return {}
