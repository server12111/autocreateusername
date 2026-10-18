"""Админка: экспорт и импорт базы данных (SQLite).

Экспорт — целостная копия «на ходу» через backup API SQLite (учитывает журнал WAL, где лежат
последние изменения; простое копирование файла bot.db их бы потеряло), сжатая gzip.
Импорт — файл проверяется (формат, целостность, таблицы бота), админ видит, что в нём,
и подтверждает. Перед заменой текущая база сохраняется в DATA_DIR/backups — последний импорт
можно откатить кнопкой. После замены бот перезапускается (systemd поднимает его сам), чтобы
аккаунты пула, настройки и кэши загрузились из новой базы.
"""

import asyncio
import gzip
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import make_url

from config import settings
from database.base import IS_SQLITE
from handlers.sections import safe_edit

log = logging.getLogger(__name__)

router = Router(name="admin_db")
router.message.filter(F.from_user.id.in_(settings.admin_ids))
router.callback_query.filter(F.from_user.id.in_(settings.admin_ids))

REQUIRED_TABLES = {"users", "settings", "payments", "mtproto_accounts", "searches_history"}
MAX_UPLOAD = 20 * 1024 * 1024  # больше Bot API скачать не даёт
MAX_UNPACKED = 1024 * 1024 * 1024  # защита от «gzip-бомбы»
KEEP_BACKUPS = 5
SQLITE_MAGIC = b"SQLite format 3\x00"
RESTART_DELAY = 1.5  # сек: успеть отправить сообщение перед перезапуском

_lock = asyncio.Lock()  # одна операция с базой за раз (два админа одновременно не смешают импорт)


class DbStates(StatesGroup):
    wait_file = State()


# ───────────────────────── файлы и проверки ─────────────────────────


def _db_path() -> str:
    return make_url(settings.DATABASE_URL).database or ""


def _work_dir(name: str) -> str:
    path = os.path.join(settings.DATA_DIR, name)
    os.makedirs(path, exist_ok=True)
    return path


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _snapshot(dest: str) -> None:
    """Целостная копия живой базы (включая изменения из журнала WAL)."""
    src = sqlite3.connect(_db_path(), timeout=30)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _restore(src_path: str) -> None:
    """Записывает содержимое src_path в живую базу через backup API — атомарно для читателей:
    при ошибке база остаётся прежней."""
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(_db_path(), timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _stats(path: str) -> dict:
    """Проверяет, что файл — целая база этого бота, и считает основные данные. ValueError — нет."""
    with open(path, "rb") as f:
        if f.read(16) != SQLITE_MAGIC:
            raise ValueError("это не база SQLite")
    con = sqlite3.connect(path)
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("файл базы повреждён (integrity_check)")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = REQUIRED_TABLES - tables
        if missing:
            raise ValueError("это база не от этого бота — нет таблиц: " + ", ".join(sorted(missing)))

        def count(table: str) -> int:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if table in tables else 0

        return {
            "users": count("users"),
            "payments": count("payments"),
            "accounts": count("mtproto_accounts"),
            "searches": count("searches_history"),
            "traps": count("nickname_traps"),
            "stock": count("free_names"),
        }
    except sqlite3.DatabaseError as e:
        raise ValueError(f"файл не читается как база: {e}") from e
    finally:
        con.close()


def _gunzip(src: str, dest: str) -> None:
    written = 0
    with gzip.open(src, "rb") as fin, open(dest, "wb") as fout:
        while chunk := fin.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UNPACKED:
                raise ValueError("распакованный файл слишком большой")
            fout.write(chunk)


def _gzip(src: str, dest: str) -> None:
    with open(src, "rb") as fin, gzip.open(dest, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout)


def _remove(*paths: str) -> None:
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def _backups() -> list[str]:
    folder = _work_dir("backups")
    files = [os.path.join(folder, f) for f in os.listdir(folder) if f.startswith("before_import_") and f.endswith(".db")]
    return sorted(files)


def _trim_backups() -> None:
    for path in _backups()[:-KEEP_BACKUPS]:
        _remove(path)


def _fmt_stats(s: dict) -> str:
    return (f"👥 Пользователей: <b>{s['users']}</b> · 💳 Платежей: <b>{s['payments']}</b>\n"
            f"🤖 Аккаунтов пула: <b>{s['accounts']}</b> · 🔍 Поисков: <b>{s['searches']}</b>\n"
            f"🪤 Ловушек: <b>{s['traps']}</b> · 📦 Ников в запасе: <b>{s['stock']}</b>")


def _size(path: str) -> str:
    total = sum(os.path.getsize(p) for p in (path, path + "-wal") if os.path.exists(p))
    return f"{total / 1024 / 1024:.1f} МБ"


def _kb(*rows: tuple[str, str]):
    kb = InlineKeyboardBuilder()
    for text, data in rows:
        kb.button(text=text, callback_data=data)
    kb.adjust(1)
    return kb.as_markup()


# ───────────────────────── экраны ─────────────────────────


async def _screen() -> tuple[str, object]:
    current = await asyncio.to_thread(_stats, _db_path())
    backups = _backups()
    text = (
        "💾 <b>БАЗА ДАННЫХ</b>\n\n"
        f"{_fmt_stats(current)}\n"
        f"📁 Размер: <b>{_size(_db_path())}</b>\n\n"
        "📤 <b>Экспорт</b> — пришлю файл с полной копией базы\n"
        "📥 <b>Импорт</b> — заменить базу присланным файлом (текущая сохранится, импорт можно откатить)"
    )
    rows = [("📤 Экспорт базы", "adm:db:export"), ("📥 Импорт базы", "adm:db:import")]
    if backups:
        rows.append(("↩️ Откатить последний импорт", "adm:db:undo"))
    rows.append(("🔙 В админку", "adm:home"))
    return text, _kb(*rows)


@router.callback_query(F.data == "adm:db")
async def db_menu(call: CallbackQuery, state: FSMContext) -> None:
    if not IS_SQLITE:
        await call.answer("Экспорт и импорт работают только с SQLite", show_alert=True)
        return
    await state.clear()
    await call.answer()
    await safe_edit(call, *await _screen())


@router.callback_query(F.data == "adm:db:export")
async def db_export(call: CallbackQuery, bot: Bot) -> None:
    if _lock.locked():
        await call.answer("Уже идёт операция с базой — подождите", show_alert=True)
        return
    async with _lock:
        await call.answer("⏳ Готовлю копию базы…")
        tmp = os.path.join(_work_dir("export_tmp"), f"bot_{_stamp()}.db")
        packed = tmp + ".gz"
        try:
            await asyncio.to_thread(_snapshot, tmp)
            stats = await asyncio.to_thread(_stats, tmp)
            await asyncio.to_thread(_gzip, tmp, packed)
            with open(packed, "rb") as f:
                data = f.read()
            await bot.send_document(
                call.from_user.id,
                BufferedInputFile(data, filename=os.path.basename(packed)),
                caption=(
                    f"💾 <b>Копия базы</b> · {datetime.now():%d.%m.%Y %H:%M}\n\n{_fmt_stats(stats)}\n\n"
                    "⚠️ В файле сессии аккаунтов пула (полный доступ к ним) и данные пользователей — "
                    "не пересылайте его"
                ),
            )
        except Exception as e:
            log.exception("Экспорт базы не удался")
            await call.message.answer(f"⚠️ Экспорт не удался: {e}")
        finally:
            _remove(tmp, packed)


@router.callback_query(F.data == "adm:db:import")
async def db_import_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(DbStates.wait_file)
    await call.answer()
    await safe_edit(
        call,
        "📥 <b>ИМПОРТ БАЗЫ</b>\n\n"
        "Пришлите файл базы <b>документом</b>: <code>.db</code> или сжатый <code>.db.gz</code> "
        "(например, из экспорта), до 20 МБ\n\n"
        "Перед заменой я покажу, что в файле, и спрошу подтверждение",
        _kb(("❌ Отмена", "adm:db")),
    )


@router.message(DbStates.wait_file, F.document)
async def db_import_file(message: Message, state: FSMContext, bot: Bot) -> None:
    doc = message.document
    if doc.file_size and doc.file_size > MAX_UPLOAD:
        await message.answer("⚠️ Файл больше 20 МБ — Telegram не даст боту его скачать. Пришлите сжатый .db.gz",
                             reply_markup=_kb(("❌ Отмена", "adm:db")))
        return
    old = (await state.get_data()).get("import_path")
    _remove(old)
    folder = _work_dir("import_tmp")
    raw = os.path.join(folder, f"upload_{_stamp()}_{message.from_user.id}")
    path = raw + ".db"
    wait = await message.answer("⏳ Проверяю файл…")
    try:
        await bot.download(doc, destination=raw)
        with open(raw, "rb") as f:
            gz = f.read(2) == b"\x1f\x8b"
        if gz:
            await asyncio.to_thread(_gunzip, raw, path)
            _remove(raw)
        else:
            os.replace(raw, path)
        new = await asyncio.to_thread(_stats, path)
        current = await asyncio.to_thread(_stats, _db_path())
    except ValueError as e:
        _remove(raw, path)
        await wait.edit_text(f"⚠️ Этот файл нельзя импортировать: {e}\n\nПришлите другой файл",
                             reply_markup=_kb(("❌ Отмена", "adm:db")))
        return
    except Exception as e:
        log.exception("Импорт базы: не удалось прочитать файл")
        _remove(raw, path)
        await wait.edit_text(f"⚠️ Не удалось прочитать файл: {e}", reply_markup=_kb(("❌ Отмена", "adm:db")))
        return
    await state.set_state(None)
    await state.update_data(import_path=path)
    await wait.edit_text(
        "📥 <b>ПРОВЕРКА ПРОЙДЕНА</b>\n\n"
        f"<b>В файле:</b>\n{_fmt_stats(new)}\n\n"
        f"<b>Сейчас в боте:</b>\n{_fmt_stats(current)}\n\n"
        "⚠️ Текущая база будет <b>полностью заменена</b> этой. Перед заменой сохраню копию текущей — "
        "импорт можно будет откатить. После замены бот перезапустится (~20 сек)",
        reply_markup=_kb(("✅ Заменить базу", "adm:db:confirm"), ("❌ Отмена", "adm:db:cancel")),
    )


@router.message(DbStates.wait_file)
async def db_import_not_file(message: Message) -> None:
    await message.answer("Пришлите файл базы документом (📎 → Файл)", reply_markup=_kb(("❌ Отмена", "adm:db")))


@router.callback_query(F.data == "adm:db:cancel")
async def db_import_cancel(call: CallbackQuery, state: FSMContext) -> None:
    _remove((await state.get_data()).get("import_path"))
    await state.clear()
    await call.answer("Импорт отменён")
    await safe_edit(call, *await _screen())


async def _replace_and_restart(
    call: CallbackQuery, dispatcher: Dispatcher, source: str, note: str, on_done=None
) -> None:
    """Сохраняет текущую базу, записывает source в живую базу и перезапускает бота.
    on_done вызывается только после успешной замены."""
    backup = os.path.join(_work_dir("backups"), f"before_import_{_stamp()}.db")
    await asyncio.to_thread(_snapshot, backup)
    try:
        await asyncio.to_thread(_restore, source)
    except Exception:
        _remove(backup)  # замена не прошла — база прежняя, эта копия не нужна
        raise
    if on_done:
        on_done()
    stats = await asyncio.to_thread(_stats, _db_path())
    _trim_backups()
    log.warning("База данных заменена админом %s (%s); копия прежней: %s", call.from_user.id, note, backup)
    await call.message.edit_text(
        f"✅ <b>База заменена</b> ({note})\n\n{_fmt_stats(stats)}\n\n"
        f"Копия прежней базы: <code>{os.path.basename(backup)}</code>\n"
        "🔄 Бот перезапускается — через ~20 сек отправьте /admin"
    )
    # Перезапуск: останавливаем polling, main() завершается, systemd (Restart=always) поднимает бота заново
    await asyncio.sleep(RESTART_DELAY)
    await dispatcher.stop_polling()


@router.callback_query(F.data == "adm:db:confirm")
async def db_import_confirm(call: CallbackQuery, state: FSMContext, dispatcher: Dispatcher) -> None:
    path = (await state.get_data()).get("import_path")
    if not path or not os.path.exists(path):
        await state.clear()
        await call.answer("Файл импорта не найден — пришлите его заново", show_alert=True)
        await safe_edit(call, *await _screen())
        return
    if _lock.locked():
        await call.answer("Уже идёт операция с базой — подождите", show_alert=True)
        return
    async with _lock:
        await call.answer("⏳ Заменяю базу…")
        try:
            await asyncio.to_thread(_stats, path)  # файл могли подменить — проверяем ещё раз
            await state.clear()
            await _replace_and_restart(call, dispatcher, path, "импорт")
        except Exception as e:
            log.exception("Импорт базы не удался")
            await call.message.answer(f"⚠️ Импорт не удался, база осталась прежней: {e}")
        finally:
            _remove(path)


@router.callback_query(F.data == "adm:db:undo")
async def db_undo_prompt(call: CallbackQuery) -> None:
    backups = _backups()
    if not backups:
        await call.answer("Копий до импорта нет", show_alert=True)
        return
    last = backups[-1]
    stats = await asyncio.to_thread(_stats, last)
    await call.answer()
    await safe_edit(
        call,
        "↩️ <b>ОТКАТ ИМПОРТА</b>\n\n"
        f"Вернуть базу, какой она была перед последним импортом (<code>{os.path.basename(last)}</code>):\n"
        f"{_fmt_stats(stats)}\n\n⚠️ Всё, что изменилось после импорта, пропадёт. Бот перезапустится",
        _kb(("✅ Откатить", "adm:db:undo:ok"), ("❌ Отмена", "adm:db")),
    )


@router.callback_query(F.data == "adm:db:undo:ok")
async def db_undo(call: CallbackQuery, dispatcher: Dispatcher) -> None:
    backups = _backups()
    if not backups:
        await call.answer("Копий до импорта нет", show_alert=True)
        return
    if _lock.locked():
        await call.answer("Уже идёт операция с базой — подождите", show_alert=True)
        return
    last = backups[-1]
    async with _lock:
        await call.answer("⏳ Откатываю…")
        tmp = last + ".restore"
        try:
            # Копия: сама резервная копия остаётся нетронутой, а перед откатом сохранится текущая база
            shutil.copyfile(last, tmp)
            await asyncio.to_thread(_stats, tmp)
            # Использованную копию удаляем только после успешной замены — иначе следующий откат
            # снова вернул бы её же
            await _replace_and_restart(call, dispatcher, tmp, "откат импорта", on_done=lambda: _remove(last))
        except Exception as e:
            log.exception("Откат импорта не удался")
            await call.message.answer(f"⚠️ Откат не удался, база осталась прежней: {e}")
        finally:
            _remove(tmp)


def cleanup_tmp() -> None:
    """Удаляет брошенные временные файлы экспорта/импорта (старше часа) — вызывается при запуске."""
    for name in ("export_tmp", "import_tmp"):
        folder = os.path.join(settings.DATA_DIR, name)
        if not os.path.isdir(folder):
            continue
        for f in os.listdir(folder):
            p = os.path.join(folder, f)
            if time.time() - os.path.getmtime(p) > 3600:
                _remove(p)
