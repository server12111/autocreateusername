"""Запас заранее найденных свободных ников: пользователь получает ник почти мгновенно.

Пока ботом никто не ищет, фоновый воркер ищет свободные 5- и 6-буквенные ники и складывает
их в таблицу free_names, а в остальное свободное время перепроверяет старые запасы.
Как только пользователь запускает поиск, воркер встаёт на паузу, чтобы не отнимать
лимиты проверок. При выдаче ник ещё раз проверяется — и только потом отдаётся.

База не разрастается: в запасе не больше TARGET ников на длину (всего ~80 строк),
выданные и занятые ники сразу удаляются.
"""

import asyncio
import logging
import time
from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import delete, func, select

from database.base import session_maker
from database.models import FreeName, utcnow
from services.username_checker import CheckerUnavailable, CheckResult, UsernameChecker, generate_nice, is_valid_username

log = logging.getLogger(__name__)


class _Activity:
    """Сколько пользовательских поисков идёт сейчас и когда закончился последний."""

    def __init__(self) -> None:
        self.running = 0
        self.last_end = 0.0

    @contextmanager
    def user_search(self):
        self.running += 1
        try:
            yield
        finally:
            self.running -= 1
            self.last_end = time.monotonic()

    def idle(self, quiet_sec: float) -> bool:
        return self.running == 0 and time.monotonic() - self.last_end >= quiet_sec


activity = _Activity()


class FreeNamePool:
    TARGET = {5: 40, 6: 40}  # максимум ников в запасе для каждой длины
    QUIET_SEC = 15  # столько секунд без пользовательских поисков — бот «простаивает»
    RECHECK_AFTER = timedelta(minutes=30)  # ник в запасе перепроверяется не реже, чем раз в 30 мин
    BATCH = 3  # проверок за раз — небольшими порциями, чтобы быстро уступить место пользователю
    PAUSE = 1.5  # пауза между порциями, сек (бережём лимиты аккаунтов)

    def __init__(self, checker: UsernameChecker):
        self.checker = checker
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    # ───────────────────────── запас в БД ─────────────────────────

    async def stock(self) -> dict[int, int]:
        async with session_maker() as s:
            rows = await s.execute(select(FreeName.length, func.count()).group_by(FreeName.length))
            return {length: count for length, count in rows.all()}

    async def _store(self, names: list[str]) -> None:
        async with session_maker() as s:
            for name in names:
                if not await s.get(FreeName, name):
                    s.add(FreeName(username=name, length=len(name)))
            await s.commit()

    async def _remove(self, names: list[str]) -> None:
        if names:
            async with session_maker() as s:
                await s.execute(delete(FreeName).where(FreeName.username.in_(names)))
                await s.commit()

    async def _touch(self, names: list[str]) -> None:
        async with session_maker() as s:
            for name in names:
                row = await s.get(FreeName, name)
                if row:
                    row.checked_at = utcnow()
            await s.commit()

    async def _claim(self, name: str) -> bool:
        """Забирает ник из запаса. False — его уже забрал параллельный поиск."""
        async with session_maker() as s:
            res = await s.execute(delete(FreeName).where(FreeName.username == name))
            await s.commit()
            return res.rowcount == 1

    # ───────────────────────── выдача ─────────────────────────

    async def take(self, length: int, exclude: set[str]) -> CheckResult | None:
        """Выдаёт ник из запаса, предварительно перепроверив его. None — подходящего нет."""
        async with session_maker() as s:
            candidates = list(
                (
                    await s.scalars(
                        select(FreeName.username)
                        .where(FreeName.length == length)
                        .order_by(FreeName.checked_at.desc())
                    )
                ).all()
            )
        tries = 0
        for name in candidates:
            if name in exclude or not await self._claim(name):
                continue
            res = await self.checker.check(name)
            if res.is_free:
                return res
            if res.status == "unknown":
                # Сервисы не ответили — ник не потерян, возвращаем в запас
                await self._store([name])
                return None
            tries += 1
            if tries >= 3:
                break
        return None

    # ───────────────────────── фоновый воркер ─────────────────────────

    async def _run(self) -> None:
        await asyncio.sleep(5)  # даём боту спокойно стартовать
        while True:
            try:
                if not activity.idle(self.QUIET_SEC):
                    await asyncio.sleep(2)
                    continue
                # Сначала перепроверка устаревших (её мало), иначе пока запас не полон — она бы не шла
                if await self._recheck_step() or await self._refill_step():
                    await asyncio.sleep(self.PAUSE)
                else:
                    await asyncio.sleep(10)  # запас полный и свежий
            except asyncio.CancelledError:
                raise
            except CheckerUnavailable:
                await asyncio.sleep(60)
            except Exception:
                log.exception("Запас ников: ошибка фоновой работы")
                await asyncio.sleep(30)

    async def _refill_step(self) -> bool:
        """Одна порция поиска для длины, где запас меньше всего. False — запас полный."""
        stock = await self.stock()
        need = [n for n, target in self.TARGET.items() if stock.get(n, 0) < target]
        if not need:
            return False
        length = min(need, key=lambda n: stock.get(n, 0) / self.TARGET[n])
        async with session_maker() as s:
            have = set((await s.scalars(select(FreeName.username))).all())
        names = set()
        for _ in range(self.BATCH * 50):
            if len(names) >= self.BATCH:
                break
            n = generate_nice(length)
            if is_valid_username(n) and n not in have:
                names.add(n)
        results = await asyncio.gather(*(self.checker.check(n) for n in names))
        free = [r.username for r in results if r.is_free]
        if free:
            await self._store(free[: self.TARGET[length] - stock.get(length, 0)])
            log.info("Запас ников: +%s (%d букв)", ", ".join(free), length)
        if results and all(r.status == "unknown" for r in results):
            raise CheckerUnavailable()
        return True

    async def _recheck_step(self) -> bool:
        """Перепроверяет самые давно проверенные ники. False — перепроверять нечего."""
        border = utcnow() - self.RECHECK_AFTER
        async with session_maker() as s:
            stale = list(
                (
                    await s.scalars(
                        select(FreeName.username)
                        .where(FreeName.checked_at < border)
                        .order_by(FreeName.checked_at)
                        .limit(self.BATCH)
                    )
                ).all()
            )
        if not stale:
            return False
        results = await asyncio.gather(*(self.checker.check(n) for n in stale))
        await self._touch([r.username for r in results if r.is_free or r.status == "unknown"])
        gone = [r.username for r in results if not r.is_free and r.status != "unknown"]
        if gone:
            await self._remove(gone)
            log.info("Запас ников: заняты, удалены — %s", ", ".join(gone))
        return True
