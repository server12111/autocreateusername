import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings
from database import crud
from database.base import engine, init_db, session_maker
from database.models import msk, utcnow
from handlers import admin, admin_accounts, admin_botohub, admin_style, nickname_battle, profile, referrals, search, shop, start
from keyboards import inline
from middlewares.captcha_mw import CaptchaMiddleware
from middlewares.db_middleware import DbSessionMiddleware
from services.free_pool import FreeNamePool
from services.http import close_session
from services.mtproto_pool import BotResolver, MTProtoPool
from services.nickname_sniper import run_sniper_cycle
from services.payment_service import discount_line, plan_prices, poll_crypto_invoices
from services.ui_style import UiStyleMiddleware
from services.username_checker import UsernameChecker

log = logging.getLogger("usersearch")


async def cleanup_db() -> None:
    async with session_maker() as session:
        removed = await crud.cleanup_old_records(session)
    log.info(
        "Чистка БД: удалено записей истории %d, старых ловушек %d, старых счетов %d",
        removed["history"], removed["traps"], removed["invoices"],
    )


async def premium_expiry(bot: Bot) -> None:
    async with session_maker() as session:
        await premium_reminders(bot, session)
        expired = await crud.expire_premiums(session)
        for uid in expired:
            user = await crud.get_user(session, uid)
            prices, pct = plan_prices(user)
            text = (
                "⌛️ Срок вашей Premium-подписки истёк.\n\n"
                "Продлите её, чтобы снова искать без ограничений и пользоваться Ловушкой."
            )
            if pct:
                text += f"\n\n{discount_line(user)}"
            try:
                await bot.send_message(uid, text, reply_markup=inline.renew_kb(prices, pct))
            except Exception:
                pass


async def premium_reminders(bot: Bot, session) -> None:
    """Напоминание о скором окончании Premium + скидка на продление (настройка renew_discount_pct)."""
    pct = min(await crud.get_setting_int(session, "renew_discount_pct"), 90)
    for user in await crud.premium_reminder_due(session):
        await crud.mark_premium_reminded(session, user, pct)
        hours = max(1, round((user.premium_until - utcnow()).total_seconds() / 3600))
        prices, active = plan_prices(user)
        text = f"⏳ <b>Ваш Premium закончится через {hours} ч.</b> ({msk(user.premium_until):%d.%m %H:%M} МСК)\n\n"
        if active:
            text += (
                f"🎁 Продлите сейчас со скидкой <b>{active}%</b> — она действует до "
                f"{msk(user.discount_until):%d.%m %H:%M} МСК, даже если Premium уже закончится.\n\n"
                "Срок продления добавится к текущему — ничего не сгорит."
            )
        else:
            text += "Продлите заранее — срок добавится к текущему, ничего не сгорит."
        try:
            await bot.send_message(user.tg_id, text, reply_markup=inline.renew_kb(prices, active))
        except Exception as e:
            log.info("Напоминание о Premium не доставлено %s: %r", user.tg_id, e)


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)

    await init_db()

    pool = MTProtoPool(settings.SESSIONS_DIR, settings.API_ID, settings.API_HASH)
    await pool.init_pool()
    resolver = BotResolver(
        os.path.join(settings.DATA_DIR, "bot_mtproto"), settings.API_ID, settings.API_HASH, settings.BOT_TOKEN
    )
    await resolver.start()
    checker = UsernameChecker(pool, resolver=resolver)

    bot = Bot(settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session.middleware(UiStyleMiddleware())
    checker.bot = bot

    dp = Dispatcher(storage=MemoryStorage())
    dp["pool"] = pool
    dp["checker"] = checker
    name_pool = FreeNamePool(checker)
    dp["name_pool"] = name_pool

    db_mw = DbSessionMiddleware()
    captcha_mw = CaptchaMiddleware()
    for observer in (dp.message, dp.callback_query, dp.pre_checkout_query):
        observer.outer_middleware(db_mw)
    dp.message.outer_middleware(captcha_mw)
    dp.callback_query.outer_middleware(captcha_mw)


    dp.include_routers(
        admin.router,
        admin_accounts.router,
        admin_style.router,
        admin_botohub.router,
        start.router,
        shop.payments_router,
        search.router,
        shop.router,
        profile.router,
        referrals.router,
        nickname_battle.router,
        start.fallback_router,
    )

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(cleanup_db, "cron", hour=0, minute=0)  # 00:00 UTC = 03:00 МСК
    scheduler.add_job(premium_expiry, "interval", minutes=5, args=[bot])
    scheduler.add_job(
        poll_crypto_invoices, "interval", seconds=30, args=[bot], max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        run_sniper_cycle, "interval", seconds=settings.SNIPER_INTERVAL, args=[bot, checker],
        max_instances=1, coalesce=True,
    )
    scheduler.start()
    name_pool.start()

    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Главное меню"),
    ])
    me = await bot.me()
    log.info("Бот @%s запущен. MTProto-аккаунтов: %d", me.username, pool.size)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await name_pool.stop()
        await pool.close()
        await resolver.close()
        await close_session()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
