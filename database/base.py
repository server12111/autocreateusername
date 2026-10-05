import logging
import os

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")
if IS_SQLITE:
    os.makedirs("data", exist_ok=True)

engine = create_async_engine(settings.DATABASE_URL, echo=False, pool_pre_ping=True)
session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


if IS_SQLITE:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        # WAL: чтение не блокирует запись; busy_timeout: ждём вместо ошибки «database is locked»
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


def _add_missing_columns(conn) -> None:
    """Лёгкая автомиграция: добавляет новые колонки в существующие таблицы, не трогая данные."""
    insp = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            ddl = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col.type.compile(conn.dialect)}'
            if col.server_default is not None:
                ddl += f" DEFAULT {col.server_default.arg}"
            conn.execute(text(ddl))
            log.info("БД: добавлена колонка %s.%s", table.name, col.name)


async def init_db() -> None:
    from database import models  # noqa: F401  (регистрация таблиц)
    from database.crud import ensure_default_settings

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
    async with session_maker() as session:
        await ensure_default_settings(session)
