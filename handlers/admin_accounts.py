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
from telethon.sessions import StringSession
from telethon.tl.functions.auth import ResendCodeRequest
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
    stats = pool.stats()
    if stats:
        for st in stats:
            if st["frozen"]:
                status = "🧊 заморожен Telegram — проверять ники не может"
            else:
                status = "🟢 готов" if not st["flood_left"] else f"⏳ FloodWait {st['flood_left']} сек"
            text += (
                f"• <code>{html.escape(st['name'])}</code> — {status}\n"
                f"   за час: {st['hour']}/{pool.hour_limit} · FloodWait за сутки: {st['floods_24h']} · "
                f"темп: раз в {st['interval']} сек\n"
            )
            kb.button(text=f"🗑 {st['name']}"[:60], callback_data=f"adm:acc:del:{st['name']}"[:64])
        text += (
            f"\nДоступно: <b>{pool.alive}/{pool.size}</b>\n"
            f"<i>Темп и лимит — в ⚙️ Настройках. Частые FloodWait — увеличьте паузу или добавьте аккаунты</i>"
        )
    else:
        text += (
            "Аккаунтов нет — бот работает в резервном режиме (t.me + Fragment), "
            "это менее точно\n\nДобавьте 2–3 запасных аккаунта"
        )
    if not settings.API_ID or not settings.API_HASH:
        text += "\n\n⚠️ В .env не заданы API_ID / API_HASH — добавление аккаунтов недоступно"
    kb.button(text="📱 Войти по номеру", callback_data="adm:acc:phone")
    kb.button(text="📎 Загрузить .session", callback_data="adm:acc:upload")
    kb.button(text="🔄 Обновить", callback_data="adm:acc")
    kb.button(text="🔙 В админку", callback_data="adm:home")
    kb.adjust(1)
    return text, kb.as_markup()


async def _drop_login(admin_id: int) -> None:
    data = _logins.pop(admin_id, None)
    if not data:
        return
    try:
        await data["client"].disconnect()
    except Exception:
        pass


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
        "📱 Отправьте номер телефона аккаунта в международном формате, например <code>+79991234567</code>\n\n"
        "💡 Используйте запасной аккаунт, не основной",
        _cancel_kb(),
    )


@router.message(AccStates.phone, F.text)
async def acc_phone_input(message: Message, pool: MTProtoPool, state: FSMContext) -> None:
    phone = "+" + re.sub(r"\D", "", message.text)
    if len(phone) < 8:
        await message.answer("⚠️ Некорректный номер. Попробуйте ещё раз:", reply_markup=_cancel_kb())
        return
    await _drop_login(message.from_user.id)
    file = f"{phone[1:]}.session"
    if pool.has(file):
        await state.clear()
        await message.answer("ℹ️ Этот аккаунт уже есть в пуле", reply_markup=_accounts_screen(pool)[1])
        return
    # Сессия в памяти: после входа pool.add_client сохранит её в БД, файл не нужен
    client = TelegramClient(StringSession(), settings.API_ID, settings.API_HASH)
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
    where, can_send = _code_destination(sent)
    if not can_send:
        await client.disconnect()
        await message.answer(f"❌ {where}", reply_markup=_cancel_kb())
        return
    _logins[message.from_user.id] = {"client": client, "phone": phone, "hash": sent.phone_code_hash, "file": file}
    await state.set_state(AccStates.code)
    await message.answer(_code_prompt(where), reply_markup=_code_kb(sent))


# Куда Telegram отправил код входа — по типу ответа auth.sentCode
_CODE_WHERE = {
    "SentCodeTypeApp": "в приложение Telegram — сообщением в чате <b>«Telegram»</b> (с синей галочкой) "
                       "на устройстве, где этот аккаунт уже открыт. Это не SMS",
    "SentCodeTypeSms": "по <b>SMS</b> на номер",
    "SentCodeTypeSmsWord": "по <b>SMS</b> на номер (код — слово из сообщения)",
    "SentCodeTypeSmsPhrase": "по <b>SMS</b> на номер (код — фраза из сообщения)",
    "SentCodeTypeFirebaseSms": "по <b>SMS</b> на номер",
    "SentCodeTypeCall": "<b>звонком</b> — робот продиктует код",
    "SentCodeTypeFlashCall": "<b>звонком-сбросом</b> — код в последних цифрах номера, с которого позвонят",
    "SentCodeTypeMissedCall": "<b>пропущенным звонком</b> — код в последних цифрах номера, с которого позвонят",
    "SentCodeTypeFragmentSms": "на <b>Fragment</b> (fragment.com → My Assets → номер) — номер куплен на Fragment",
    "SentCodeTypeEmailCode": "на <b>почту</b>, привязанную к аккаунту для входа",
}


def _code_destination(sent) -> tuple[str, bool]:
    """(куда отправлен код, удалось ли отправить)."""
    kind = type(sent).__name__
    if kind == "SentCodePaymentRequired":
        return ("Telegram требует оплату за отправку SMS этому номеру и код не отправил. Войдите в аккаунт "
                "через приложение Telegram и загрузите файл .session (кнопка «📎 Загрузить .session»)"), False
    if kind == "SentCodeSuccess":
        return "Telegram сразу авторизовал вход без кода — попробуйте добавить аккаунт ещё раз", False
    code_type = type(getattr(sent, "type", None)).__name__
    if code_type == "SentCodeTypeSetUpEmailRequired":
        return ("Telegram требует сначала привязать к этому аккаунту почту для входа и код не отправил. "
                "Откройте аккаунт в приложении: Настройки → Конфиденциальность → Почта для входа, "
                "затем попробуйте снова — или загрузите .session"), False
    return _CODE_WHERE.get(code_type, "в Telegram или по SMS"), True


def _code_prompt(where: str) -> str:
    return (
        f"📨 Код отправлен {where}\n\n"
        "⚠️ Отправьте код <b>через пробелы или дефисы</b>, например <code>1 2 3 4 5</code> — "
        "иначе Telegram заблокирует код как «пересланный»"
    )


def _code_kb(sent):
    kb = InlineKeyboardBuilder()
    if getattr(sent, "next_type", None):  # Telegram разрешает отправить код другим способом
        kb.button(text="🔁 Отправить другим способом", callback_data="adm:acc:resend")
    kb.button(text="❌ Отмена", callback_data="adm:acc:cancel")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data == "adm:acc:resend")
async def acc_resend_code(call: CallbackQuery) -> None:
    data = _logins.get(call.from_user.id)
    if not data:
        await call.answer("Вход устарел — начните заново", show_alert=True)
        return
    try:
        sent = await data["client"](ResendCodeRequest(data["phone"], data["hash"]))
    except Exception as e:
        # Например, Telegram ещё не разрешает повтор (нужно подождать) или способов больше нет
        await call.answer(f"Не получилось: {str(e)[:150]}", show_alert=True)
        return
    data["hash"] = sent.phone_code_hash
    where, _ = _code_destination(sent)
    await call.answer("Код отправлен заново")
    await safe_edit(call, _code_prompt(where), _code_kb(sent))


async def _finish_login(message: Message, pool: MTProtoPool, state: FSMContext) -> None:
    data = _logins.pop(message.from_user.id)
    client: TelegramClient = data["client"]
    me = await client.get_me()
    await pool.add_client(client, data["file"])
    await state.clear()
    await message.answer(
        f"✅ Аккаунт <b>{html.escape(me.first_name or '')}</b> ({data['phone']}) добавлен в пул",
        reply_markup=_accounts_screen(pool)[1],
    )


@router.message(AccStates.code, F.text)
async def acc_code_input(message: Message, pool: MTProtoPool, state: FSMContext) -> None:
    data = _logins.get(message.from_user.id)
    if not data:
        await state.clear()
        await message.answer("Сессия входа устарела, начните заново")
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
        await message.answer("⌛️ Код истёк (возможно, его отправили без пробелов). Начните заново",
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
        await message.answer("Сессия входа устарела, начните заново")
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
        "📎 Отправьте файл <b>.session</b> (формат Telethon). Можно несколько по очереди\n\n"
        "Сессия должна быть создана с тем же API_ID/API_HASH или совместимым",
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
        await message.answer(f"ℹ️ Аккаунт {name} уже в пуле")
        return
    # Временный файл в подпапке (импорт старых сессий её не сканирует): сессия переносится в БД,
    # сам файл после этого не нужен
    upload_dir = os.path.join(settings.SESSIONS_DIR, "_upload")
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, f"{message.from_user.id}_{name}")
    await bot.download(message.document, destination=path)
    try:
        ok, info = await pool.add_session_file(path, name)
    finally:
        _safe_remove(path)
    if ok:
        await message.answer(f"✅ {html.escape(name)} добавлен: {html.escape(info)}\n\nМожно отправить ещё или вернуться:",
                             reply_markup=_accounts_screen(pool)[1])
    else:
        await message.answer(f"❌ {html.escape(name)} не подключён: {html.escape(info)}", reply_markup=_cancel_kb())
