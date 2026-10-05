import asyncio
import logging
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
from handlers import admin, admin_accounts, admin_style, nickname_battle, profile, referrals, search, shop, start
from middlewares.captcha_mw import CaptchaMiddleware
from middlewares.db_middleware import DbSessionMiddleware
from middlewares.subscription_mw import SubscriptionMiddleware
from services.http import close_session
from services.mtproto_pool import BotResolver, MTProtoPool
from services.nickname_sniper import run_sniper_cycle
from services.ui_style import UiStyleMiddleware
from services.username_checker import UsernameChecker

log = logging.getLogger("usersearch")


async def daily_reset() -> None:
    async with session_maker() as session:
        await crud.reset_daily_limits(session)
    log.info("Ежедневные лимиты сброшены")


async def premium_expiry(bot: Bot) -> None:
    async with session_maker() as session:
        expired = await crud.expire_premiums(session)
    for uid in expired:
        try:
            await bot.send_message(
                uid,
                "⌛️ Срок вашей Premium-подписки истёк.\n\nПродлите её в разделе «🛒 Магазин», "
                "чтобы снова искать без ограничений и пользоваться Ловушкой.",
            )
        except Exception:
            pass


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
    resolver = BotResolver("data/bot_mtproto", settings.API_ID, settings.API_HASH, settings.BOT_TOKEN)
    await resolver.start()
    checker = UsernameChecker(pool, resolver=resolver)

    bot = Bot(settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session.middleware(UiStyleMiddleware())
    checker.bot = bot

    dp = Dispatcher(storage=MemoryStorage())
    dp["pool"] = pool
    dp["checker"] = checker

    db_mw = DbSessionMiddleware()
    captcha_mw = CaptchaMiddleware()
    for observer in (dp.message, dp.callback_query, dp.pre_checkout_query):
        observer.outer_middleware(db_mw)
    dp.message.outer_middleware(captcha_mw)
    dp.callback_query.outer_middleware(captcha_mw)

    op_mw = SubscriptionMiddleware()
    for r in (search.router, shop.router, profile.router, referrals.router, nickname_battle.router):
        r.message.middleware(op_mw)
        r.callback_query.middleware(op_mw)

    dp.include_routers(
        admin.router,
        admin_accounts.router,
        admin_style.router,
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
    scheduler.add_job(daily_reset, "cron", hour=0, minute=0)
    scheduler.add_job(premium_expiry, "interval", minutes=5, args=[bot])
    scheduler.add_job(
        run_sniper_cycle, "interval", seconds=settings.SNIPER_INTERVAL, args=[bot, checker],
        max_instances=1, coalesce=True,
    )
    scheduler.start()

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
