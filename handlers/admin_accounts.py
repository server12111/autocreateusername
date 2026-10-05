"""Админка: управление аккаунтами MTProto-пула (вход по номеру или загрузка .session)."""

import gc
import html
import os
import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from config import settings
from handlers.sections import safe_edit
from services.mtproto_pool import MTProtoPool

router = Router(name="admin_accounts")
router.message.filter(F.from_user.id.in_(settings.admin_ids))
router.callback_query.filter(F.from_user.id.in_(settings.admin_ids))


class AccStates(StatesGroup):
    phone = State()
    code = State()
    password = State()
    upload = State()


# admin_id -> {"client": TelegramClient, "phone": str, "hash": str, "file": str}
_logins: dict[int, dict] = {}


def _safe_remove(path: str) -> None:
    gc.collect()  # закрыть брошенные sqlite-соединения Telethon (Windows держит файл)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="adm:acc:cancel")
    return kb.as_markup()


def _accounts_screen(pool: MTProtoPool):
    text = "🤖 <b>АККАУНТЫ ДЛЯ ПРОВЕРКИ</b>\n\n"
    kb = InlineKeyboardBuilder()
    info = pool.info()
    if info:
        for name, ready, wait in info:
            status = "🟢 готов" if ready else f"⏳ FloodWait {wait} сек"
            text += f"• <code>{html.escape(name)}</code> — {status}\n"
            kb.button(text=f"🗑 {name}"[:60], callback_data=f"adm:acc:del:{name}"[:64])
        text += f"\nДоступно: <b>{pool.alive}/{pool.size}</b>"
    else:
        text += (
            "Аккаунтов нет — бот работает в резервном режиме (t.me + Fragment), "
            "это менее точно.\n\nДобавьте 2–3 запасных аккаунта."
        )
    if not settings.API_ID or not settings.API_HASH:
        text += "\n\n⚠️ В .env не заданы API_ID / API_HASH — добавление аккаунтов недоступно."
    kb.button(text="📱 Войти по номеру", callback_data="adm:acc:phone")
    kb.button(text="📎 Загрузить .session", callback_data="adm:acc:upload")
    kb.button(text="🔄 Обновить", callback_data="adm:acc")
    kb.button(text="🔙 В админку", callback_data="adm:home")
    kb.adjust(1)
    return text, kb.as_markup()


async def _drop_login(admin_id: int, delete_file: bool = True) -> None:
    data = _logins.pop(admin_id, None)
    if not data:
        return
    try:
        await data["client"].disconnect()
    except Exception:
        pass
    if delete_file:
        _safe_remove(os.path.join(settings.SESSIONS_DIR, data["file"]))


@router.callback_query(F.data == "adm:acc")
async def acc_menu(call: CallbackQuery, pool: MTProtoPool, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await safe_edit(call, *_accounts_screen(pool))


@router.callback_query(F.data == "adm:acc:cancel")
async def acc_cancel(call: CallbackQuery, pool: MTProtoPool, state: FSMContext) -> None:
    await _drop_login(call.from_user.id)
    await acc_menu(call, pool, state)


@router.callback_query(F.data.startswith("adm:acc:del:"))
async def acc_delete(call: CallbackQuery, pool: MTProtoPool) -> None:
    name = call.data.split(":", 3)[3]
    match = next((n for n, _, _ in pool.info() if n.startswith(name)), name)
    await pool.remove(match)
    await call.answer("🗑 Аккаунт удалён из пула")
    await safe_edit(call, *_accounts_screen(pool))


# ───────────────────────── Вход по номеру ─────────────────────────


@router.callback_query(F.data == "adm:acc:phone")
async def acc_phone(call: CallbackQuery, state: FSMContext) -> None:
    if not settings.API_ID or not settings.API_HASH:
        await call.answer("Сначала задайте API_ID и API_HASH в .env", show_alert=True)
        return
    await state.set_state(AccStates.phone)
    await call.answer()
    await safe_edit(
        call,
        "📱 Отправьте номер телефона аккаунта в международном формате, например <code>+79991234567</code>.\n\n"
        "💡 Используйте запасной аккаунт, не основной.",
        _cancel_kb(),
    )


@router.message(AccStates.phone, F.text)
async def acc_phone_input(message: Message, pool: MTProtoPool, state: FSMContext) -> None:
    phone = "+" + re.sub(r"\D", "", message.text)
    if len(phone) < 8:
        await message.answer("⚠️ Некорректный номер. Попробуйте ещё раз:", reply_markup=_cancel_kb())
        return
    await _drop_login(message.from_user.id)
    os.makedirs(settings.SESSIONS_DIR, exist_ok=True)
    file = f"{phone[1:]}.session"
    if pool.has(file):
        await state.clear()
        await message.answer("ℹ️ Этот аккаунт уже есть в пуле.", reply_markup=_accounts_screen(pool)[1])
        return
    path = os.path.join(settings.SESSIONS_DIR, phone[1:])
    client = TelegramClient(path, settings.API_ID, settings.API_HASH)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await client.disconnect()
        await message.answer("❌ Telegram не принял этот номер. Попробуйте другой:", reply_markup=_cancel_kb())
        return
    except Exception as e:
        await client.disconnect()
        await message.answer(f"❌ Ошибка: <code>{html.escape(str(e))}</code>", reply_markup=_cancel_kb())
        return
    _logins[message.from_user.id] = {"client": client, "phone": phone, "hash": sent.phone_code_hash, "file": file}
    await state.set_state(AccStates.code)
    await message.answer(
        "📨 Код отправлен в Telegram (или по SMS) на этот аккаунт.\n\n"
        "⚠️ Отправьте код <b>через пробелы или дефисы</b>, например <code>1 2 3 4 5</code> — "
        "иначе Telegram заблокирует код как «пересланный».",
        reply_markup=_cancel_kb(),
    )


async def _finish_login(message: Message, pool: MTProtoPool, state: FSMContext) -> None:
    data = _logins.pop(message.from_user.id)
    client: TelegramClient = data["client"]
    me = await client.get_me()
    await pool.add_client(client, data["file"])
    await state.clear()
    await message.answer(
        f"✅ Аккаунт <b>{html.escape(me.first_name or '')}</b> ({data['phone']}) добавлен в пул.",
        reply_markup=_accounts_screen(pool)[1],
    )


@router.message(AccStates.code, F.text)
async def acc_code_input(message: Message, pool: MTProtoPool, state: FSMContext) -> None:
    data = _logins.get(message.from_user.id)
    if not data:
        await state.clear()
        await message.answer("Сессия входа устарела, начните заново.")
        return
    code = re.sub(r"\D", "", message.text)
    try:
        await data["client"].sign_in(data["phone"], code, phone_code_hash=data["hash"])
    except SessionPasswordNeededError:
        await state.set_state(AccStates.password)
        await message.answer("🔐 На аккаунте включена двухэтапная проверка. Отправьте облачный пароль:", reply_markup=_cancel_kb())
        return
    except PhoneCodeInvalidError:
        await message.answer("❌ Неверный код. Попробуйте ещё раз:", reply_markup=_cancel_kb())
        return
    except PhoneCodeExpiredError:
        await _drop_login(message.from_user.id)
        await state.clear()
        await message.answer("⌛️ Код истёк (возможно, его отправили без пробелов). Начните заново.",
                             reply_markup=_accounts_screen(pool)[1])
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка: <code>{html.escape(str(e))}</code>", reply_markup=_cancel_kb())
        return
    await _finish_login(message, pool, state)


@router.message(AccStates.password, F.text)
async def acc_password_input(message: Message, pool: MTProtoPool, state: FSMContext) -> None:
    data = _logins.get(message.from_user.id)
    password = message.text
    try:
        await message.delete()
    except Exception:
        pass
    if not data:
        await state.clear()
        await message.answer("Сессия входа устарела, начните заново.")
        return
    try:
        await data["client"].sign_in(password=password)
    except PasswordHashInvalidError:
        await message.answer("❌ Неверный пароль. Попробуйте ещё раз:", reply_markup=_cancel_kb())
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка: <code>{html.escape(str(e))}</code>", reply_markup=_cancel_kb())
        return
    await _finish_login(message, pool, state)


# ───────────────────────── Загрузка .session ─────────────────────────


@router.callback_query(F.data == "adm:acc:upload")
async def acc_upload(call: CallbackQuery, state: FSMContext) -> None:
    if not settings.API_ID or not settings.API_HASH:
        await call.answer("Сначала задайте API_ID и API_HASH в .env", show_alert=True)
        return
    await state.set_state(AccStates.upload)
    await call.answer()
    await safe_edit(
        call,
        "📎 Отправьте файл <b>.session</b> (формат Telethon). Можно несколько по очереди.\n\n"
        "Сессия должна быть создана с тем же API_ID/API_HASH или совместимым.",
        _cancel_kb(),
    )


@router.message(AccStates.upload, F.document)
async def acc_upload_file(message: Message, bot: Bot, pool: MTProtoPool) -> None:
    name = os.path.basename(message.document.file_name or "")
    if not name.endswith(".session"):
        await message.answer("⚠️ Нужен файл с расширением .session")
        return
    name = re.sub(r"[^\w.-]", "_", name)
    if pool.has(name):
        await message.answer(f"ℹ️ Аккаунт {name} уже в пуле.")
        return
    os.makedirs(settings.SESSIONS_DIR, exist_ok=True)
    path = os.path.join(settings.SESSIONS_DIR, name)
    await bot.download(message.document, destination=path)
    ok, info = await pool.add_session_file(name)
    if ok:
        await message.answer(f"✅ {html.escape(name)} добавлен: {html.escape(info)}\n\nМожно отправить ещё или вернуться:",
                             reply_markup=_accounts_screen(pool)[1])
    else:
        _safe_remove(path)
        await message.answer(f"❌ {html.escape(name)} не подключён: {html.escape(info)}", reply_markup=_cancel_kb())
