from aiogram import Bot
from aiogram.types import LabeledPrice, SuccessfulPayment
from sqlalchemy.ext.asyncio import AsyncSession

from config import PREMIUM_PLANS, SEARCH_PACKS
from database import crud
from database.models import User


def describe(payload: str) -> tuple[str, str, int] | None:
    """payload -> (заголовок, описание, цена в Stars)."""
    if payload in PREMIUM_PLANS:
        days, price = PREMIUM_PLANS[payload]
        return f"Premium на {days} дн.", f"UserSearch Premium: безлимитный поиск на {days} дн.", price
    if payload in SEARCH_PACKS:
        count, price = SEARCH_PACKS[payload]
        return f"{count} поисков", f"Пакет из {count} дополнительных поисков юзернеймов", price
    return None


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


async def process_payment(session: AsyncSession, user: User, payment: SuccessfulPayment) -> str:
    """Начисляет покупку и возвращает текст чека."""
    is_new = await crud.add_payment(
        session, user.tg_id, payment.invoice_payload, payment.total_amount, payment.telegram_payment_charge_id
    )
    if not is_new:
        return "ℹ️ Этот платёж уже был обработан."

    payload = payment.invoice_payload
    lines = [
        "🧾 <b>ЧЕК ОБ ОПЛАТЕ</b>",
        "",
    ]
    if payload in PREMIUM_PLANS:
        days, _ = PREMIUM_PLANS[payload]
        until = await crud.add_premium_days(session, user, days)
        lines += [
            f"📦 Товар: <b>Premium на {days} дн.</b>",
            f"💳 Оплачено: <b>{payment.total_amount} ⭐️</b>",
            f"💎 Premium активен до: <b>{until:%d.%m.%Y %H:%M}</b> UTC",
        ]
    else:
        count, _ = SEARCH_PACKS[payload]
        user.paid_searches_left += count
        await session.commit()
        lines += [
            f"📦 Товар: <b>{count} поисков</b>",
            f"💳 Оплачено: <b>{payment.total_amount} ⭐️</b>",
            f"🔍 Доступно купленных поисков: <b>{user.paid_searches_left}</b>",
        ]
    lines += ["", f"🆔 Транзакция: <code>{payment.telegram_payment_charge_id}</code>", "", "Спасибо за покупку! 💙"]
    return "\n".join(lines)
