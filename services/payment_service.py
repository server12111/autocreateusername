import logging
import time
from decimal import Decimal

from aiogram import Bot
from aiogram.types import LabeledPrice, SuccessfulPayment
from sqlalchemy.ext.asyncio import AsyncSession

from config import PREMIUM_PLANS, PRICES_USD, SEARCH_PACKS
from database import crud
from database.base import session_maker
from database.models import CryptoInvoice, User, msk
from services import crypto_pay

log = logging.getLogger(__name__)


def describe(payload: str) -> tuple[str, str, int] | None:
    """payload -> (заголовок, описание, цена в Stars)."""
    if payload in PREMIUM_PLANS:
        days, price = PREMIUM_PLANS[payload]
        return f"Premium на {days} дн.", f"UserSearch Premium: безлимитный поиск на {days} дн.", price
    if payload in SEARCH_PACKS:
        count, price = SEARCH_PACKS[payload]
        return f"{count} поисков", f"Пакет из {count} дополнительных поисков юзернеймов", price
    return None


def price_usd(payload: str) -> str | None:
    return PRICES_USD.get(payload) if describe(payload) else None


async def send_stars_invoice(bot: Bot, chat_id: int, payload: str) -> bool:
    info = describe(payload)
    if not info:
        return False
    title, description, price = info
    await bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=price)],
        provider_token="",
    )
    return True


def validate_pre_checkout(payload: str, total_amount: int, currency: str) -> bool:
    info = describe(payload)
    return bool(info) and currency == "XTR" and info[2] == total_amount


async def _grant(session: AsyncSession, user: User, payload: str, paid: str, tx_id: str) -> str:
    """Начисляет товар и возвращает текст чека."""
    lines = ["🧾 <b>ЧЕК ОБ ОПЛАТЕ</b>", ""]
    if payload in PREMIUM_PLANS:
        days, _ = PREMIUM_PLANS[payload]
        until = await crud.add_premium_days(session, user, days)
        lines += [
            f"📦 Товар: <b>Premium на {days} дн.</b>",
            f"💳 Оплачено: <b>{paid}</b>",
            f"💎 Premium активен до: <b>{msk(until):%d.%m.%Y %H:%M}</b> МСК",
        ]
    else:
        count, _ = SEARCH_PACKS[payload]
        user.paid_searches_left += count
        await session.commit()
        lines += [
            f"📦 Товар: <b>{count} поисков</b>",
            f"💳 Оплачено: <b>{paid}</b>",
            f"🔍 Доступно купленных поисков: <b>{user.paid_searches_left}</b>",
        ]
    lines += ["", f"🆔 Транзакция: <code>{tx_id}</code>", "", "Спасибо за покупку! 💙"]
    return "\n".join(lines)


async def process_payment(session: AsyncSession, user: User, payment: SuccessfulPayment) -> str:
    """Начисляет покупку за Stars и возвращает текст чека."""
    is_new = await crud.add_payment(
        session, user.tg_id, payment.invoice_payload, payment.total_amount, payment.telegram_payment_charge_id
    )
    if not is_new:
        return "ℹ️ Этот платёж уже был обработан."
    return await _grant(
        session, user, payment.invoice_payload, f"{payment.total_amount} ⭐️", payment.telegram_payment_charge_id
    )


# ───────────────────────── CryptoBot / xRocket ─────────────────────────


async def get_or_create_crypto_invoice(
    session: AsyncSession, user: User, provider: str, payload: str
) -> CryptoInvoice | None:
    """Счёт в долларах на товар. None — товар не найден. CryptoPayError — платёжка не ответила."""
    amount = price_usd(payload)
    if not amount or provider not in crypto_pay.enabled_providers():
        return None
    existing = await crud.find_open_crypto_invoice(session, user.tg_id, provider, payload, amount)
    if existing:
        return existing
    title, description, _ = describe(payload)
    created = await crypto_pay.create_invoice(
        provider, amount, f"{title}. {description}", f"u{user.tg_id}-{payload}-{time.time_ns()}"
    )
    return await crud.add_crypto_invoice(
        session, provider, created.invoice_id, user.tg_id, payload, amount, created.pay_url, crypto_pay.INVOICE_TTL
    )


async def complete_crypto_invoice(session: AsyncSession, inv: CryptoInvoice) -> str | None:
    """Счёт оплачен: начисляет товар один раз. Возвращает чек или None, если счёт уже обработан."""
    if not await crud.claim_paid_crypto_invoice(session, inv, int(Decimal(inv.amount_usd) * 100)):
        return None
    try:
        user = await crud.get_user(session, inv.user_id)
        paid = f"${inv.amount_usd} через {crypto_pay.provider_name(inv.provider)}"
        # Статус счёта, платёж и сама покупка сохраняются одним commit внутри _grant
        return await _grant(session, user, inv.payload, paid, f"{inv.provider}:{inv.invoice_id}")
    except Exception:
        await session.rollback()
        raise


async def check_crypto_invoice(session: AsyncSession, inv: CryptoInvoice) -> tuple[str, str | None]:
    """Спрашивает статус у платёжки. -> (статус, чек если только что начислили)."""
    if inv.status != "active":
        return inv.status, None
    status = (await crypto_pay.get_statuses(inv.provider, [inv.invoice_id])).get(inv.invoice_id, "active")
    if status == "paid":
        return "paid", await complete_crypto_invoice(session, inv)
    if status in ("expired", "cancelled"):
        await crud.set_crypto_invoice_status(session, inv.id, "expired")
        return "expired", None
    return "active", None


async def poll_crypto_invoices(bot: Bot) -> None:
    """Фоновая проверка: начисляет оплаченные счета, даже если пользователь не нажал «Проверить»."""
    async with session_maker() as session:
        await crud.expire_stale_crypto_invoices(session)
        pending = await crud.pending_crypto_invoices(session)
        by_provider: dict[str, list[CryptoInvoice]] = {}
        for inv in pending:
            by_provider.setdefault(inv.provider, []).append(inv)

        for provider, invoices in by_provider.items():
            if provider not in crypto_pay.enabled_providers():
                continue
            for i in range(0, len(invoices), 100):
                chunk = invoices[i : i + 100]
                statuses = await crypto_pay.get_statuses(provider, [inv.invoice_id for inv in chunk])
                for inv in chunk:
                    status = statuses.get(inv.invoice_id)
                    if status == "paid":
                        try:
                            receipt = await complete_crypto_invoice(session, inv)
                        except Exception:
                            log.exception("Не удалось начислить оплату по счёту %s:%s", provider, inv.invoice_id)
                            await session.rollback()
                            continue
                        if receipt:
                            await _notify(bot, inv.user_id, receipt)
                    elif status in ("expired", "cancelled"):
                        await crud.set_crypto_invoice_status(session, inv.id, "expired")


async def _notify(bot: Bot, user_id: int, receipt: str) -> None:
    from keyboards.inline import back_kb

    try:
        await bot.send_message(user_id, receipt, reply_markup=back_kb("menu:main", "🏠 Главное меню"))
    except Exception as e:
        log.warning("Не удалось отправить чек пользователю %s: %r", user_id, e)
