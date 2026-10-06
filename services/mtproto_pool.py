import asyncio
import logging
import os
import random
import time
from collections import deque

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    FrozenMethodInvalidError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UsernameInvalidError,
    UsernameOccupiedError,
    UsernameNotOccupiedError,
    UsernamePurchaseAvailableError,
)
from telethon.sessions import SQLiteSession, StringSession
from telethon.tl.functions.account import CheckUsernameRequest
from telethon.tl.functions.contacts import ResolveUsernameRequest

from database import crud
from database.base import session_maker

log = logging.getLogger(__name__)

# Ошибки, после которых сессия аккаунта мертва навсегда (в отличие от сетевых сбоев и FloodWait)
DEAD_SESSION_ERRORS = (
    AuthKeyUnregisteredError,
    AuthKeyDuplicatedError,
    SessionRevokedError,
    SessionExpiredError,
    UserDeactivatedError,
    UserDeactivatedBanError,
)


class _Worker:
    """Аккаунт пула со своим темпом: не чаще раза в interval × slow секунд и не больше hour_limit в час."""

    def __init__(self, name: str, client: TelegramClient):
        self.name = name
        self.client = client
        self.busy_until = 0.0  # unix time, до которого аккаунт во FloodWait
        self.lock = asyncio.Lock()
        self.next_at = 0.0  # раньше этого момента аккаунт следующий запрос не делает (темп + брони)
        self.slow = 1.0  # множитель темпа: после FloodWait растёт, после удачных запросов плавно падает
        self.hour_start = time.time()
        self.hour_count = 0
        self.floods: deque[float] = deque()  # моменты FloodWait за последние сутки
        self.total = 0
        self.frozen = False  # заморожен Telegram (FROZEN_METHOD_INVALID)

    @property
    def ready(self) -> bool:
        return time.time() >= self.busy_until

    def used_this_hour(self) -> int:
        if time.time() - self.hour_start >= 3600:
            self.hour_start, self.hour_count = time.time(), 0
        return self.hour_count

    def floods_24h(self) -> int:
        while self.floods and time.time() - self.floods[0] > 86400:
            self.floods.popleft()
        return len(self.floods)


class BotResolver:
    """MTProto-клиент под токеном бота: contacts.resolveUsername.

    Бот не может вызывать account.checkUsername, но resolve различает:
    занят (найден peer) / свободен (USERNAME_NOT_OCCUPIED) / зарезервирован или запрещён (USERNAME_INVALID).
    """

    def __init__(self, session_path: str, api_id: int, api_hash: str, bot_token: str):
        self.session_path = session_path
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_token = bot_token
        self.client: TelegramClient | None = None
        self.busy_until = 0.0
        self.lock = asyncio.Lock()
        self.last_call = 0.0
        self.last_error = ""  # для диагностики в админке

    @property
    def flood_left(self) -> int:
        """Сколько секунд ещё длится FloodWait (0 — нет)."""
        return max(0, int(self.busy_until - time.time()))

    MIN_INTERVAL = 1.0  # пауза между запросами бота: у ботов лимит на resolveUsername особенно жёсткий
    MAX_WAIT = 20  # короткий FloodWait пережидаем, длинный — отдаём «unavailable»

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def start(self) -> None:
        if not self.api_id or not self.api_hash:
            return
        try:
            os.makedirs(os.path.dirname(self.session_path) or ".", exist_ok=True)
            # receive_updates=False — апдейты получает только aiogram, клиент нужен лишь для запросов
            client = TelegramClient(
                self.session_path, self.api_id, self.api_hash, flood_sleep_threshold=0, receive_updates=False
            )
            await client.start(bot_token=self.bot_token)
            self.client = client
            log.info("MTProto: проверка через бота включена")
        except Exception as e:
            log.warning("MTProto: не удалось запустить клиент бота: %s", e)

    async def close(self) -> None:
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    async def resolve(self, username: str) -> str:
        """occupied | free | reserved | unavailable"""
        if not self.client:
            return "unavailable"
        async with self.lock:
            for _ in range(3):
                wait = self.busy_until - time.time()
                if wait > self.MAX_WAIT:
                    return "unavailable"
                pause = max(wait, self.last_call + self.MIN_INTERVAL - time.time())
                if pause > 0:
                    await asyncio.sleep(pause)
                self.last_call = time.time()
                try:
                    await self.client(ResolveUsernameRequest(username=username))
                    return "occupied"
                except UsernameNotOccupiedError:
                    return "free"
                except UsernameInvalidError:
                    return "reserved"
                except FloodWaitError as e:
                    self.busy_until = time.time() + e.seconds + 1
                    log.info("MTProto-бот во FloodWait на %d сек", e.seconds)
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"[:200]
                    log.debug("MTProto-бот: ошибка resolve %s: %s", username, e)
                    return "unavailable"
        return "unavailable"


class MTProtoPool:
    """Пул Telethon-аккаунтов с бережным темпом запросов, часовым лимитом и обходом FloodWait."""

    BACKGROUND_SHARE = 0.6  # фоновым задачам (запас ников) — не больше 60% часового лимита аккаунта
    MAX_SLOW = 8.0  # максимум замедления после серии FloodWait
    FROZEN_PAUSE = 6 * 3600  # замороженный аккаунт перепробуем раз в 6 часов (вдруг заморозку сняли)

    def __init__(self, sessions_dir: str, api_id: int, api_hash: str):
        self.sessions_dir = sessions_dir
        self.api_id = api_id
        self.api_hash = api_hash
        self.workers: list[_Worker] = []
        self.interval = 2.0  # сек между запросами одного аккаунта (настройка acc_interval_sec)
        self.hour_limit = 300  # запросов в час на аккаунт (настройка acc_hour_limit)

    @property
    def size(self) -> int:
        return len(self.workers)

    @property
    def alive(self) -> int:
        return sum(1 for w in self.workers if w.ready)

    def _quota(self, background: bool) -> float:
        return self.hour_limit * (self.BACKGROUND_SHARE if background else 1.0)

    def has_capacity(self, background: bool = False) -> bool:
        """Есть ли аккаунт, готовый принять запрос (не во FloodWait и не исчерпал свою долю лимита)."""
        quota = self._quota(background)
        return any(w.ready and w.used_this_hour() < quota for w in self.workers)

    async def init_pool(self) -> None:
        if not self.api_id or not self.api_hash:
            log.warning("API_ID/API_HASH не заданы — MTProto-пул отключён, используется резервная проверка")
            return
        await self._import_session_files()
        async with session_maker() as s:
            accounts = [(a.name, a.session) for a in await crud.list_mtproto_accounts(s)]
        for name, session_string in accounts:
            client = self._client(session_string)
            try:
                await client.connect()
                if await client.is_user_authorized():
                    self.workers.append(_Worker(name, client))
                    log.info("MTProto: аккаунт %s подключён", name)
                else:
                    # Сессию отозвали — в админке такой аккаунт не виден, поэтому удаляем сами
                    log.warning("MTProto: аккаунт %s больше не авторизован — удалён из базы", name)
                    await client.disconnect()
                    await self.remove(name)
            except Exception as e:
                log.warning("MTProto: не удалось подключить %s: %s", name, e)
        log.info("MTProto-пул: %d аккаунтов", len(self.workers))

    def _client(self, session_string: str = "") -> TelegramClient:
        return TelegramClient(StringSession(session_string), self.api_id, self.api_hash, flood_sleep_threshold=0)

    async def _import_session_files(self) -> None:
        """Переносит старые .session-файлы из sessions/ в БД (файлы не трогаем — на всякий случай)."""
        if not os.path.isdir(self.sessions_dir):
            return
        async with session_maker() as s:
            known = {a.name for a in await crud.list_mtproto_accounts(s)}
            for file in sorted(os.listdir(self.sessions_dir)):
                if not file.endswith(".session") or file in known:
                    continue
                session_string = self._read_session_file(os.path.join(self.sessions_dir, file))
                if session_string:
                    await crud.save_mtproto_account(s, file, session_string)
                    log.info("MTProto: сессия %s перенесена в базу данных", file)

    @staticmethod
    def _read_session_file(path: str) -> str:
        """Telethon .session (SQLite) -> строка StringSession. Пустая строка — сессия без ключа или битая."""
        try:
            sqlite_session = SQLiteSession(path)  # путь с «.session» на конце
            try:
                return StringSession.save(sqlite_session)
            finally:
                sqlite_session.close()
        except Exception as e:
            log.warning("MTProto: не удалось прочитать %s: %s", path, e)
            return ""

    def has(self, name: str) -> bool:
        return any(w.name == name for w in self.workers)

    async def add_client(self, client: TelegramClient, name: str) -> None:
        """Добавляет авторизованный клиент в пул и сохраняет его сессию в БД (вход через админку)."""
        client.flood_sleep_threshold = 0
        async with session_maker() as s:
            await crud.save_mtproto_account(s, name, StringSession.save(client.session))
        self.workers = [w for w in self.workers if w.name != name]
        self.workers.append(_Worker(name, client))

    async def add_session_file(self, path: str, name: str) -> tuple[bool, str]:
        """Подключает загруженный .session: переводит в строку, проверяет и сохраняет в БД."""
        session_string = self._read_session_file(path)
        if not session_string:
            return False, "файл не похож на сессию Telethon или в нём нет ключа авторизации"
        client = self._client(session_string)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return False, "сессия не авторизована"
            me = await client.get_me()
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            return False, f"{type(e).__name__}: {e}"
        await self.add_client(client, name)
        return True, f"{me.first_name or ''} (+{me.phone or '?'})"

    async def remove(self, name: str) -> bool:
        worker = next((w for w in self.workers if w.name == name), None)
        if worker:
            self.workers.remove(worker)
            try:
                await worker.client.disconnect()
            except Exception:
                pass
        async with session_maker() as s:
            deleted = await crud.delete_mtproto_account(s, name)
        # Старый файл тоже убираем, иначе при запуске он снова импортируется в БД
        path = os.path.join(self.sessions_dir, name)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        return deleted or worker is not None

    def info(self) -> list[tuple[str, bool, int]]:
        """[(файл, готов, секунд FloodWait осталось)]"""
        now = time.time()
        return [(w.name, w.ready, max(0, int(w.busy_until - now))) for w in self.workers]

    def stats(self) -> list[dict]:
        """Нагрузка аккаунтов для админки."""
        now = time.time()
        return [
            {
                "name": w.name,
                "flood_left": max(0, int(w.busy_until - now)),
                "hour": w.used_this_hour(),
                "floods_24h": w.floods_24h(),
                "interval": round(self.interval * w.slow, 1),
                "frozen": w.frozen,
            }
            for w in self.workers
        ]

    async def close(self) -> None:
        for w in self.workers:
            try:
                await w.client.disconnect()
            except Exception:
                pass

    def _pick(self, background: bool) -> tuple[_Worker, float] | None:
        """Аккаунт, который раньше всех сможет сделать запрос, и забронированное на нём время.

        Бронь сразу сдвигает очередь аккаунта, поэтому одновременные запросы расходятся по разным
        аккаунтам, а не выстраиваются к первому. Бронь сразу учитывается и в часовом лимите.
        """
        quota = self._quota(background)
        candidates = [w for w in self.workers if w.ready and w.used_this_hour() < quota]
        if not candidates:
            return None
        now = time.time()
        worker = min(candidates, key=lambda w: max(w.next_at, now))
        slot = max(worker.next_at, now)
        # Темп со случайным разбросом, чтобы запросы не шли ровной «машинной» сеткой
        worker.next_at = slot + self.interval * worker.slow * random.uniform(0.85, 1.25)
        worker.hour_count += 1
        return worker, slot

    async def check_username(self, username: str, background: bool = False) -> dict:
        """
        status: free | occupied | fragment_only | invalid | no_clients | error
        background — фоновая задача: ей достаётся только BACKGROUND_SHARE часового лимита аккаунтов.
        """
        clean = username.lstrip("@")
        for _ in range(max(1, len(self.workers))):
            picked = self._pick(background)
            if not picked:
                return {"available": False, "status": "no_clients"}
            worker, slot = picked
            async with worker.lock:
                if slot > time.time():
                    await asyncio.sleep(slot - time.time())
                if not worker.ready or worker not in self.workers:
                    worker.hour_count = max(0, worker.hour_count - 1)  # бронь не использована
                    continue  # пока ждали, аккаунт словил FloodWait или был удалён
                worker.total += 1
                try:
                    result = await worker.client(CheckUsernameRequest(username=clean))
                    status = "free" if result else "occupied"
                except UsernameOccupiedError:
                    status = "occupied"
                except UsernamePurchaseAvailableError:
                    status = "fragment_only"
                except UsernameInvalidError:
                    status = "invalid"
                except FloodWaitError as e:
                    worker.busy_until = time.time() + e.seconds + 1
                    worker.slow = min(worker.slow * 2, self.MAX_SLOW)
                    worker.floods.append(time.time())
                    log.info("MTProto: %s во FloodWait на %d сек, темп замедлен до %.1f сек",
                             worker.name, e.seconds, self.interval * worker.slow)
                    continue
                except FrozenMethodInvalidError:
                    # Аккаунт заморожен Telegram за нарушения: почти все запросы, включая проверку ников,
                    # недоступны. Не удаляем (заморозку могут снять), но надолго убираем из работы
                    worker.frozen = True
                    worker.busy_until = time.time() + self.FROZEN_PAUSE
                    log.warning("MTProto: аккаунт %s заморожен Telegram — пауза %d ч",
                                worker.name, self.FROZEN_PAUSE // 3600)
                    continue
                except DEAD_SESSION_ERRORS as e:
                    # Аккаунт разлогинен, удалён или забанен — больше он не заработает
                    log.warning("MTProto: аккаунт %s отключён (%s) — удалён из пула и базы",
                                worker.name, type(e).__name__)
                    status = "dead"
                except Exception as e:
                    log.warning("MTProto: ошибка %s на %s: %s", type(e).__name__, worker.name, e)
                    worker.busy_until = time.time() + 30
                    continue
            if status == "dead":
                await self.remove(worker.name)
                continue
            worker.slow = max(1.0, worker.slow * 0.95)  # удачный запрос — понемногу возвращаем обычный темп
            worker.frozen = False  # заморозку сняли — аккаунт снова в строю
            return {"available": status == "free", "status": status}
        return {"available": False, "status": "no_clients"}
