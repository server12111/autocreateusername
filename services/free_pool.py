"""Запас заранее найденных свободных ников: пользователь получает ник почти мгновенно.

Фоновый воркер держит по несколько свободных 5- и 6-буквенных ников. При выдаче ник
перепроверяется (за секунды он мог уйти), и только потом отдаётся пользователю.
"""

import asyncio
import logging
import time

from services.username_checker import CheckerUnavailable, CheckResult, UsernameChecker, generate_nice

log = logging.getLogger(__name__)


class FreeNamePool:
    SIZES = {5: 6, 6: 6}  # сколько ников держать про запас для каждой длины
    TTL = 15 * 60  # ник старше 15 минут выбрасываем — его могли занять

    def __init__(self, checker: UsernameChecker):
        self.checker = checker
        self.items: dict[int, list[tuple[str, float]]] = {n: [] for n in self.SIZES}
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def _fresh(self, length: int) -> list[tuple[str, float]]:
        now = time.time()
        self.items[length] = [(n, t) for n, t in self.items[length] if now - t < self.TTL]
        return self.items[length]

    async def _run(self) -> None:
        await asyncio.sleep(5)  # даём боту спокойно стартовать
        while True:
            try:
                need = [n for n, size in self.SIZES.items() if len(self._fresh(n)) < size]
                if not need:
                    await asyncio.sleep(10)
                    continue
                for length in need:
                    taken = {n for n, _ in self.items[length]}
                    res, _ = await self.checker.find_free(
                        lambda: generate_nice(length), exclude=taken, max_seconds=120
                    )
                    if res:
                        self.items[length].append((res.username, time.time()))
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except CheckerUnavailable:
                await asyncio.sleep(60)
            except Exception:
                log.exception("Запас ников: ошибка пополнения")
                await asyncio.sleep(30)

    async def take(self, length: int, exclude: set[str]) -> CheckResult | None:
        """Выдаёт ник из запаса, предварительно перепроверив его. None — запас пуст."""
        for _ in range(3):
            items = self._fresh(length) if length in self.items else []
            idx = next((i for i, (n, _) in enumerate(items) if n not in exclude), None)
            if idx is None:
                return None
            name, _ = items.pop(idx)
            res = await self.checker.check(name)
            if res.is_free:
                return res
        return None
