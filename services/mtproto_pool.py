import asyncio
import logging
import os
import time

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    UsernameInvalidError,
    UsernameOccupiedError,
    UsernameNotOccupiedError,
    UsernamePurchaseAvailableError,
)
from telethon.tl.functions.account import CheckUsernameRequest
from telethon.tl.functions.contacts import ResolveUsernameRequest

log = logging.getLogger(__name__)


class _Worker:
    def __init__(self, name: str, client: TelegramClient):
        self.name = name
        self.client = client
        self.busy_until = 0.0  # unix time, до которого аккаунт во FloodWait
        self.lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return time.time() >= self.busy_until


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

    MIN_INTERVAL = 0.4  # пауза между запросами, чтобы не ловить FloodWait
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
                    log.debug("MTProto-бот: ошибка resolve %s: %s", username, e)
                    return "unavailable"
        return "unavailable"


class MTProtoPool:
    """Пул Telethon-аккаунтов с ротацией и обходом FloodWait."""

    def __init__(self, sessions_dir: str, api_id: int, api_hash: str):
        self.sessions_dir = sessions_dir
        self.api_id = api_id
        self.api_hash = api_hash
        self.workers: list[_Worker] = []
        self._rr = 0

    @property
    def size(self) -> int:
        return len(self.workers)

    @property
    def alive(self) -> int:
        return sum(1 for w in self.workers if w.ready)

    async def init_pool(self) -> None:
        os.makedirs(self.sessions_dir, exist_ok=True)
        if not self.api_id or not self.api_hash:
            log.warning("API_ID/API_HASH не заданы — MTProto-пул отключён, используется резервная проверка")
            return
        for file in sorted(os.listdir(self.sessions_dir)):
            if not file.endswith(".session"):
                continue
            path = os.path.join(self.sessions_dir, file[: -len(".session")])
            try:
                client = TelegramClient(path, self.api_id, self.api_hash, flood_sleep_threshold=0)
                await client.connect()
                if await client.is_user_authorized():
                    self.workers.append(_Worker(file, client))
                    log.info("MTProto: сессия %s подключена", file)
                else:
                    log.warning("MTProto: сессия %s не авторизована", file)
                    await client.disconnect()
            except Exception as e:
                log.warning("MTProto: не удалось подключить %s: %s", file, e)
        log.info("MTProto-пул: %d аккаунтов", len(self.workers))

    def has(self, file: str) -> bool:
        return any(w.name == file for w in self.workers)

    async def add_client(self, client: TelegramClient, file: str) -> None:
        """Добавляет уже авторизованный клиент в пул (вход через админку)."""
        client.flood_sleep_threshold = 0
        self.workers = [w for w in self.workers if w.name != file]
        self.workers.append(_Worker(file, client))

    async def add_session_file(self, file: str) -> tuple[bool, str]:
        """Подключает .session из папки sessions/. Возвращает (успех, описание)."""
        path = os.path.join(self.sessions_dir, file[: -len(".session")])
        client = None
        try:
            client = TelegramClient(path, self.api_id, self.api_hash, flood_sleep_threshold=0)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return False, "сессия не авторизована"
            me = await client.get_me()
        except Exception as e:
            try:
                if client:
                    await client.disconnect()
            except Exception:
                pass
            return False, f"{type(e).__name__}: {e}"
        await self.add_client(client, file)
        return True, f"{me.first_name or ''} (+{me.phone or '?'})"

    async def remove(self, file: str) -> bool:
        worker = next((w for w in self.workers if w.name == file), None)
        if worker:
            self.workers.remove(worker)
            try:
                await worker.client.disconnect()
            except Exception:
                pass
        path = os.path.join(self.sessions_dir, file)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
            return True
        return worker is not None

    def info(self) -> list[tuple[str, bool, int]]:
        """[(файл, готов, секунд FloodWait осталось)]"""
        now = time.time()
        return [(w.name, w.ready, max(0, int(w.busy_until - now))) for w in self.workers]

    async def close(self) -> None:
        for w in self.workers:
            try:
                await w.client.disconnect()
            except Exception:
                pass

    def _next_worker(self) -> _Worker | None:
        n = len(self.workers)
        for i in range(n):
            w = self.workers[(self._rr + i) % n]
            if w.ready:
                self._rr = (self._rr + i + 1) % n
                return w
        return None

    async def check_username(self, username: str) -> dict:
        """
        status: free | occupied | fragment_only | invalid | no_clients | error
        """
        clean = username.lstrip("@")
        for _ in range(max(1, len(self.workers))):
            worker = self._next_worker()
            if not worker:
                return {"available": False, "status": "no_clients"}
            try:
                async with worker.lock:
                    result = await worker.client(CheckUsernameRequest(username=clean))
                return {"available": bool(result), "status": "free" if result else "occupied"}
            except UsernameOccupiedError:
                return {"available": False, "status": "occupied"}
            except UsernamePurchaseAvailableError:
                return {"available": False, "status": "fragment_only"}
            except UsernameInvalidError:
                return {"available": False, "status": "invalid"}
            except FloodWaitError as e:
                worker.busy_until = time.time() + e.seconds + 1
                log.info("MTProto: %s во FloodWait на %d сек", worker.name, e.seconds)
                continue
            except Exception as e:
                log.warning("MTProto: ошибка %s на %s: %s", type(e).__name__, worker.name, e)
                worker.busy_until = time.time() + 30
                continue
        return {"available": False, "status": "no_clients"}
