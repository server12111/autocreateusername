import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from config import PREMIUM_PLANS
from database import crud
from database.models import User, utcnow
from handlers.sections import build_shop, safe_edit
from keyboards import inline
from services import crypto_pay
from services.payment_service import (
    check_crypto_invoice,
    describe,
    get_or_create_crypto_invoice,
    price_usd,
    process_payment,
    send_stars_invoice,
    validate_pre_checkout,
)
from texts import PACKS_TEXT, PREMIUM_TEXT

log = logging.getLogger(__name__)

router = Router(name="shop")
# Платежи обрабатываются отдельно — без проверки ОП, чтобы не потерять оплату
payments_router = Router(name="payments")


@router.callback_query(F.data == "menu:shop")
async def open_shop(call: CallbackQuery, bot: Bot, session: AsyncSession, user: User) -> None:
    await call.answer()
    await safe_edit(call, *await build_shop(session, user, bot))


@router.callback_query(F.data == "shop:premium")
async def shop_premium(call: CallbackQuery) -> None:
    await call.answer()
    await safe_edit(call, PREMIUM_TEXT, inline.premium_plans_kb())


@router.callback_query(F.data == "shop:packs")
async def shop_packs(call: CallbackQuery) -> None:
    await call.answer()
    await safe_edit(call, PACKS_TEXT, inline.packs_kb())


def _back_for(key: str) -> str:
    return "shop:premium" if key in PREMIUM_PLANS else "shop:packs"


@router.callback_query(F.data.startswith("buy:"))
async def buy(call: CallbackQuery) -> None:
    """Выбор способа оплаты: Stars, CryptoBot или xRocket."""
    key = call.data.split(":", 1)[1]
    info = describe(key)
    if not info:
        await call.answer("Товар не найден", show_alert=True)
        return
    await call.answer()
    title, _, stars = info
    usd = price_usd(key)
    providers = crypto_pay.enabled_providers() if usd else []
    lines = [
        "💳 <b>ВЫБОР СПОСОБА ОПЛАТЫ</b>",
        "",
        f"📦 Товар: <b>{title}</b>",
        f"⭐️ Telegram Stars: <b>{stars} ⭐️</b>",
    ]
    lines += [f"{crypto_pay.provider_emoji_html(p)} {crypto_pay.provider_name(p)}: <b>${usd}</b>" for p in providers]
    if providers:
        lines += ["", "В CryptoBot и xRocket можно оплатить криптовалютой (USDT, TON и др.)."]
    lines += ["", "Выберите, как удобнее оплатить:"]
    await safe_edit(call, "\n".join(lines), inline.pay_method_kb(key, stars, usd, providers, _back_for(key)))


@router.callback_query(F.data.startswith("pay:st:"))
async def pay_stars(call: CallbackQuery, bot: Bot) -> None:
    ok = await send_stars_invoice(bot, call.from_user.id, call.data.split(":", 2)[2])
    await call.answer("💳 Счёт на оплату отправлен ниже" if ok else "Товар не найден", show_alert=not ok)


@router.callback_query(F.data.regexp(r"^pay:(cb|xr):"))
async def pay_crypto(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    _, provider, key = call.data.split(":", 2)
    try:
        inv = await get_or_create_crypto_invoice(session, user, provider, key)
    except crypto_pay.CryptoPayError as e:
        log.warning("Не удалось создать счёт: %s", e)
        await call.answer(
            f"⚠️ {crypto_pay.provider_name(provider)} сейчас не отвечает. Попробуйте позже или выберите другой способ.",
            show_alert=True,
        )
        return
    if not inv:
        await call.answer("Этот способ оплаты сейчас недоступен", show_alert=True)
        return
    await call.answer()
    title, _, _ = describe(key)
    name = crypto_pay.provider_name(provider)
    left = max(1, int((inv.expires_at - utcnow()).total_seconds() // 60))
    text = (
        f"{crypto_pay.provider_emoji_html(provider)} <b>ОПЛАТА ЧЕРЕЗ {name.upper()}</b>\n\n"
        f"📦 Товар: <b>{title}</b>\n"
        f"💵 Сумма: <b>${inv.amount_usd}</b>{' (USDT)' if provider == 'xr' else ''}\n"
        f"⏳ Счёт действует: <b>{left} мин.</b>\n\n"
        f"1. Нажмите «Оплатить в {name}» и оплатите счёт.\n"
        "2. Покупка начислится автоматически в течение минуты. "
        "Чтобы не ждать, нажмите «✅ Я оплатил — проверить»."
    )
    await safe_edit(call, text, inline.crypto_invoice_kb(provider, inv.pay_url, inv.id, f"buy:{key}"))


@payments_router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    if validate_pre_checkout(query.invoice_payload, query.total_amount, query.currency):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Товар недоступен. Попробуйте оформить покупку заново.")


@payments_router.callback_query(F.data.startswith("pay:chk:"))
async def pay_check(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    inv_id = call.data.split(":", 2)[2]
    inv = await crud.get_crypto_invoice(session, int(inv_id)) if inv_id.isdigit() else None
    if not inv or inv.user_id != user.tg_id:
        await call.answer("Счёт не найден", show_alert=True)
        return
    status, receipt = await check_crypto_invoice(session, inv)
    if receipt:
        await call.answer("✅ Оплата получена!")
        await safe_edit(call, receipt, inline.back_kb("menu:main", "🏠 Главное меню"))
    elif status == "paid":
        await call.answer("✅ Этот счёт уже оплачен, покупка начислена.", show_alert=True)
    elif status == "expired":
        await call.answer("⌛️ Срок счёта истёк. Создайте новый — выберите товар заново.", show_alert=True)
    else:
        await call.answer("⏳ Оплата пока не поступила. Если вы уже оплатили — подождите минуту.", show_alert=True)


@payments_router.message(F.successful_payment)
async def successful_payment(message: Message, session: AsyncSession, user: User) -> None:
    receipt = await process_payment(session, user, message.successful_payment)
    await message.answer(receipt, reply_markup=inline.back_kb("menu:main", "🏠 Главное меню"))
