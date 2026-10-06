import logging
import time
from decimal import ROUND_HALF_UP, Decimal

from aiogram import Bot
from aiogram.types import LabeledPrice, SuccessfulPayment
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import PREMIUM_PLANS, PRICES_USD, SEARCH_PACKS
from database import crud
from database.base import session_maker
from database.models import CryptoInvoice, User, msk, utcnow
from services import crypto_pay

log = logging.getLogger(__name__)


def describe(payload: str) -> tuple[str, str, int] | None:
    """payload -> (заголовок, описание, цена в Stars)."""
    if payload in PREMIUM_PLANS:
        days, price = PREMIUM_PLANS[payload]
        return f"Premium на {days} дн.", f"NameHunter Premium: безлимитный поиск на {days} дн.", price
    if payload in SEARCH_PACKS:
        count, price = SEARCH_PACKS[payload]
        return f"{count} поисков", f"Пакет из {count} дополнительных поисков юзернеймов", price
    return None


def active_discount(user: User | None) -> int:
    """Скидка на продление Premium в процентах (0 — нет или истекла)."""
    if user and user.discount_pct and user.discount_until and user.discount_until > utcnow():
        return user.discount_pct
    return 0


def apply_discount(stars: int, pct: int) -> int:
    return max(1, round(stars * (100 - pct) / 100)) if pct else stars


def apply_discount_usd(usd: str, pct: int) -> str:
    if not pct:
        return usd
    value = (Decimal(usd) * (100 - pct) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(max(value, Decimal("0.01")))


def user_price(payload: str, user: User | None) -> tuple[int, str | None, int] | None:
    """Цена товара для пользователя: (Stars, USD или None, скидка %). Скидка — только на Premium."""
    info = describe(payload)
    if not info:
        return None
    pct = active_discount(user) if payload in PREMIUM_PLANS else 0
    usd = PRICES_USD.get(payload)
    return apply_discount(info[2], pct), (apply_discount_usd(usd, pct) if usd else None), pct


def plan_prices(user: User | None) -> tuple[dict[str, int] | None, int]:
    """Цены тарифов Premium для кнопок: ({ключ: Stars} или None без скидки, скидка %)."""
    pct = active_discount(user)
    if not pct:
        return None, 0
    return {key: apply_discount(price, pct) for key, (_, price) in PREMIUM_PLANS.items()}, pct


def discount_line(user: User | None) -> str:
    """Строка о скидке на продление для текстов (пусто, если скидки нет)."""
    pct = active_discount(user)
    if not pct:
        return ""
    return f"🎁 Ваша скидка на продление: <b>−{pct}%</b> до {msk(user.discount_until):%d.%m %H:%M} МСК"


def price_usd(payload: str, user: User | None = None) -> str | None:
    price = user_price(payload, user)
    return price[1] if price else None


async def send_stars_invoice(bot: Bot, chat_id: int, payload: str, user: User | None = None) -> bool:
    info = describe(payload)
    if not info:
        return False
    title, description, _ = info
    price, _, pct = user_price(payload, user)
    if pct:
        title += f" (−{pct}%)"
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


def validate_pre_checkout(payload: str, total_amount: int, currency: str, user: User | None = None) -> bool:
    """Принимаем текущую цену пользователя или полную (счёт мог быть выставлен до скидки)."""
    info = describe(payload)
    if not info or currency != "XTR":
        return False
    return total_amount in (info[2], user_price(payload, user)[0])


async def _grant(session: AsyncSession, user: User, payload: str, paid: str, tx_id: str) -> str:
    """Начисляет товар и возвращает текст чека."""
    lines = ["🧾 <b>ЧЕК ОБ ОПЛАТЕ</b>", ""]
    if payload in PREMIUM_PLANS:
        days, _ = PREMIUM_PLANS[payload]
        if user.discount_pct:
            user.discount_pct = 0  # скидка на продление одноразовая; сохранится тем же commit
            user.discount_until = None
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
    """Начисляет покупку за Stars и возвращает текст чека.

    Платёж и покупка сохраняются одним commit: Telegram не присылает successful_payment повторно,
    поэтому при сбое между шагами покупка не должна потеряться отдельно от записи о платеже.
    """
    charge_id = payment.telegram_payment_charge_id
    user_id = user.tg_id  # после rollback объект user устаревает — читаем ID заранее
    if not await crud.stage_payment(session, user_id, payment.invoice_payload, payment.total_amount, charge_id):
        return "ℹ️ Этот платёж уже был обработан."
    try:
        return await _grant(session, user, payment.invoice_payload, f"{payment.total_amount} ⭐️", charge_id)
    except IntegrityError:
        await session.rollback()
        return "ℹ️ Этот платёж уже был обработан."
    except Exception:
        await session.rollback()
        log.exception("Не удалось начислить оплату Stars %s пользователю %s", charge_id, user_id)
        raise


# ───────────────────────── CryptoBot / xRocket ─────────────────────────


async def get_or_create_crypto_invoice(
    session: AsyncSession, user: User, provider: str, payload: str
) -> CryptoInvoice | None:
    """Счёт в долларах на товар. None — товар не найден. CryptoPayError — платёжка не ответила."""
    amount = price_usd(payload, user)
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
        # Запоминаем только простые значения: после rollback ORM-объекты устаревают,
        # и обращение к их полям в асинхронной сессии падает (MissingGreenlet)
        by_provider: dict[str, list[tuple[int, str]]] = {}
        for inv in await crud.pending_crypto_invoices(session):
            by_provider.setdefault(inv.provider, []).append((inv.id, inv.invoice_id))

        for provider, invoices in by_provider.items():
            if provider not in crypto_pay.enabled_providers():
                continue
            for i in range(0, len(invoices), 100):
                chunk = invoices[i : i + 100]
                statuses = await crypto_pay.get_statuses(provider, [ext_id for _, ext_id in chunk])
                for inv_id, ext_id in chunk:
                    status = statuses.get(ext_id)
                    if status == "paid":
                        try:
                            inv = await crud.get_crypto_invoice(session, inv_id)
                            user_id = inv.user_id
                            receipt = await complete_crypto_invoice(session, inv)
                        except Exception:
                            log.exception("Не удалось начислить оплату по счёту %s:%s", provider, ext_id)
                            await session.rollback()
                            continue
                        if receipt:
                            await _notify(bot, user_id, receipt)
                    elif status in ("expired", "cancelled"):
                        await crud.set_crypto_invoice_status(session, inv_id, "expired")


async def _notify(bot: Bot, user_id: int, receipt: str) -> None:
    from keyboards.inline import back_kb

    try:
        await bot.send_message(user_id, receipt, reply_markup=back_kb("menu:main", "🏠 Главное меню"))
    except Exception as e:
        log.warning("Не удалось отправить чек пользователю %s: %r", user_id, e)
