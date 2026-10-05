from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from handlers.sections import build_shop, safe_edit
from keyboards import inline
from services.payment_service import process_payment, send_stars_invoice, validate_pre_checkout
from texts import PACKS_TEXT, PREMIUM_TEXT

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


@router.callback_query(F.data.startswith("buy:"))
async def buy(call: CallbackQuery, bot: Bot) -> None:
    ok = await send_stars_invoice(bot, call.from_user.id, call.data.split(":", 1)[1])
    await call.answer("💳 Счёт на оплату отправлен ниже" if ok else "Товар не найден", show_alert=not ok)


@payments_router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    if validate_pre_checkout(query.invoice_payload, query.total_amount, query.currency):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Товар недоступен. Попробуйте оформить покупку заново.")


@payments_router.message(F.successful_payment)
async def successful_payment(message: Message, session: AsyncSession, user: User) -> None:
    receipt = await process_payment(session, user, message.successful_payment)
    await message.answer(receipt, reply_markup=inline.back_kb("menu:main", "🏠 Главное меню"))
