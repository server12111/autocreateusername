import asyncio
import html
import json
import logging
import re
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import crud
from database.base import session_maker
from database.models import User
from handlers.sections import safe_edit, send_screen
from services.mtproto_pool import MTProtoPool
from services.tgrass_service import TgrassService
from texts import LINE

log = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(F.from_user.id.in_(settings.admin_ids))
router.callback_query.filter(F.from_user.id.in_(settings.admin_ids))


class AdminStates(StatesGroup):
    sp_chat = State()
    sp_link = State()
    tg_key = State()
    bc_msg = State()
    bc_buttons = State()
    user_search = State()
    user_searches = State()
    user_days = State()
    promo_new = State()
    set_value = State()


SETTING_LABELS = {
    "start_free_searches": "Бесплатных поисков новичку",
    "sponsor_bonus": "Бонус за подписку на спонсоров",
    "search_cooldown_sec": "Задержка между поисками (сек)",
    "support_url": "Ссылка на поддержку",
}


def _kb(*rows: tuple[str, str], width: int = 1) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, data in rows:
        kb.button(text=text, callback_data=data)
    kb.adjust(width)
    return kb.as_markup()


BACK = ("🔙 В админку", "adm:home")


# ───────────────────────── Главная ─────────────────────────


def _home():
    text = f"🛠 <b>ПАНЕЛЬ АДМИНИСТРАТОРА</b>\n{LINE}\n\nВыберите раздел:"
    kb = InlineKeyboardBuilder()
    for t, d in [
        ("📊 Статистика", "adm:stats"),
        ("📢 Спонсоры (ОП)", "adm:sp"),
        ("🌱 Tgrass", "adm:tg"),
        ("✉️ Рассылка", "adm:bc"),
        ("👤 Пользователи", "adm:users"),
        ("🎟 Промокоды", "adm:promo"),
        ("⚙️ Настройки", "adm:set"),
        ("🤖 Аккаунты", "adm:acc"),
        ("✨ Оформление", "adm:style"),
    ]:
        kb.button(text=t, callback_data=d)
    kb.button(text="🏠 Главное меню бота", callback_data="menu:main")
    kb.adjust(2, 2, 2, 2, 1, 1)
    return text, kb.as_markup()


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_screen(message, _home())


@router.callback_query(F.data == "adm:home")
async def adm_home(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await safe_edit(call, *_home())


# ───────────────────────── Статистика ─────────────────────────


@router.callback_query(F.data == "adm:stats")
async def adm_stats(call: CallbackQuery, session: AsyncSession, pool: MTProtoPool) -> None:
    s = await crud.get_stats(session)
    text = (
        f"📊 <b>СТАТИСТИКА</b>\n{LINE}\n\n"
        f"👥 Всего пользователей: <b>{s['total']}</b>\n"
        f"├ Новых за 24 часа: <b>{s['day']}</b>\n"
        f"├ Новых за 7 дней: <b>{s['week']}</b>\n"
        f"├ Новых за 30 дней: <b>{s['month']}</b>\n"
        f"└ Заблокировали бота: <b>{s['blocked']}</b>\n\n"
        f"💎 Активных Premium: <b>{s['premium']}</b>\n\n"
        f"🔍 Всего поисков: <b>{s['searches']}</b> (найдено свободных: {s['found']})\n"
        f"🪤 Активных ловушек: <b>{s['traps']}</b>\n\n"
        f"⭐️ Заработано Stars: <b>{s['stars']}</b> ({s['payments']} платежей)\n\n"
        f"🤖 MTProto-пул: <b>{pool.alive}/{pool.size}</b> аккаунтов доступно"
    )
    await call.answer()
    await safe_edit(call, text, _kb(("🔄 Обновить", "adm:stats"), BACK))


# ───────────────────────── Спонсоры ─────────────────────────


async def _sponsors_screen(session: AsyncSession):
    channels = await crud.get_sponsors(session, only_active=False)
    text = f"📢 <b>СОБСТВЕННЫЕ КАНАЛЫ ОП</b>\n{LINE}\n\n"
    text += "Нажмите на канал для управления.\n🟢 — активен, 🔴 — выключен." if channels else "Каналов пока нет."
    kb = InlineKeyboardBuilder()
    for ch in channels:
        kb.button(text=f"{'🟢' if ch.is_active else '🔴'} {ch.title}"[:60], callback_data=f"adm:sp:{ch.id}")
    kb.button(text="➕ Добавить канал", callback_data="adm:sp:add")
    kb.button(text=BACK[0], callback_data=BACK[1])
    kb.adjust(1)
    return text, kb.as_markup()


@router.callback_query(F.data == "adm:sp")
async def adm_sponsors(call: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await safe_edit(call, *await _sponsors_screen(session))


@router.callback_query(F.data == "adm:sp:add")
async def adm_sp_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.sp_chat)
    await call.answer()
    await safe_edit(
        call,
        "➕ <b>Добавление канала</b>\n\n"
        "Отправьте <b>@username</b> канала, его <b>ID</b> (-100…) или <b>перешлите</b> пост из канала.\n\n"
        "⚠️ Бот должен быть администратором канала, иначе проверить подписку невозможно.",
        _kb(("❌ Отмена", "adm:sp")),
    )


@router.message(AdminStates.sp_chat)
async def adm_sp_chat(message: Message, bot: Bot, state: FSMContext) -> None:
    if message.forward_from_chat:
        ref = message.forward_from_chat.id
    else:
        raw = (message.text or "").strip()
        raw = re.sub(r"^https?://t\.me/", "@", raw)
        ref = int(raw) if raw.lstrip("-").isdigit() else ("@" + raw.lstrip("@"))
    try:
        chat = await bot.get_chat(ref)
        me = await bot.me()
        member = await bot.get_chat_member(chat.id, me.id)
    except Exception as e:
        await message.answer(f"❌ Не удалось получить канал: <code>{html.escape(str(e))}</code>\n\nПопробуйте ещё раз:",
                             reply_markup=_kb(("❌ Отмена", "adm:sp")))
        return
    is_admin = member.status in ("administrator", "creator")
    await state.update_data(sp_id=chat.id, sp_title=chat.title or str(chat.id), sp_public=chat.username)
    await state.set_state(AdminStates.sp_link)
    await message.answer(
        f"Канал: <b>{html.escape(chat.title or '')}</b> (<code>{chat.id}</code>)\n"
        f"Права бота: {'✅ администратор' if is_admin else '⚠️ НЕ администратор — проверка работать не будет!'}\n\n"
        "Теперь отправьте <b>ссылку-приглашение</b> или «<code>-</code>», чтобы бот создал её сам.",
        reply_markup=_kb(("❌ Отмена", "adm:sp")),
    )


@router.message(AdminStates.sp_link, F.text)
async def adm_sp_link(message: Message, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    link = message.text.strip()
    if link == "-":
        if data.get("sp_public"):
            link = f"https://t.me/{data['sp_public']}"
        else:
            try:
                link = (await bot.create_chat_invite_link(data["sp_id"], name="UserSearch OP")).invite_link
            except Exception as e:
                await message.answer(f"❌ Не удалось создать ссылку: {html.escape(str(e))}\nОтправьте ссылку вручную:")
                return
    if not link.startswith("http"):
        await message.answer("⚠️ Ссылка должна начинаться с https://")
        return
    await crud.add_sponsor(session, data["sp_title"], data["sp_id"], link)
    await state.clear()
    await message.answer(f"✅ Канал <b>{html.escape(data['sp_title'])}</b> добавлен в ОП.")
    await send_screen(message, await _sponsors_screen(session))


@router.callback_query(F.data.regexp(r"^adm:sp:\d+$"))
async def adm_sp_open(call: CallbackQuery, session: AsyncSession) -> None:
    await adm_sp_card(call, session, int(call.data.split(":")[2]))


async def adm_sp_card(call: CallbackQuery, session: AsyncSession, sp_id: int) -> None:
    ch = next((c for c in await crud.get_sponsors(session, False) if c.id == sp_id), None)
    if not ch:
        await call.answer("Канал не найден", show_alert=True)
        return
    text = (
        f"📢 <b>{html.escape(ch.title)}</b>\n\n"
        f"ID: <code>{ch.channel_id}</code>\n"
        f"Ссылка: {ch.invite_link}\n"
        f"Статус: {'🟢 активен' if ch.is_active else '🔴 выключен'}\n"
        f"Добавлен: {ch.created_at:%d.%m.%Y}"
    )
    await call.answer()
    await safe_edit(call, text, _kb(
        ("🔴 Выключить" if ch.is_active else "🟢 Включить", f"adm:sp:tog:{ch.id}"),
        ("🧪 Проверить права бота", f"adm:sp:test:{ch.id}"),
        ("🗑 Удалить", f"adm:sp:del:{ch.id}"),
        ("🔙 К списку", "adm:sp"),
    ))


@router.callback_query(F.data.startswith("adm:sp:tog:"))
async def adm_sp_toggle(call: CallbackQuery, session: AsyncSession) -> None:
    sp_id = int(call.data.split(":")[3])
    await crud.toggle_sponsor(session, sp_id)
    await adm_sp_card(call, session, sp_id)


@router.callback_query(F.data.startswith("adm:sp:del:"))
async def adm_sp_delete(call: CallbackQuery, session: AsyncSession) -> None:
    await crud.delete_sponsor(session, int(call.data.split(":")[3]))
    await call.answer("🗑 Удалено")
    await safe_edit(call, *await _sponsors_screen(session))


@router.callback_query(F.data.startswith("adm:sp:test:"))
async def adm_sp_test(call: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    sp_id = int(call.data.split(":")[3])
    ch = next((c for c in await crud.get_sponsors(session, False) if c.id == sp_id), None)
    if not ch:
        await call.answer("Канал не найден", show_alert=True)
        return
    try:
        me = await bot.me()
        member = await bot.get_chat_member(ch.channel_id, me.id)
        ok = member.status in ("administrator", "creator")
        await call.answer("✅ Бот — администратор, проверка работает" if ok else f"⚠️ Статус бота: {getattr(member.status, 'value', member.status)}. Сделайте его админом!", show_alert=True)
    except Exception as e:
        await call.answer(f"❌ Ошибка: {str(e)[:150]}", show_alert=True)


# ───────────────────────── Tgrass ─────────────────────────


async def _tgrass_screen(session: AsyncSession):
    enabled = await crud.get_setting(session, "tgrass_enabled") == "1"
    key = await crud.get_setting(session, "tgrass_api_key")
    masked = f"{key[:4]}…{key[-4:]}" if len(key) > 10 else ("задан" if key else "не задан")
    text = (
        f"🌱 <b>ИНТЕГРАЦИЯ TGRASS</b>\n{LINE}\n\n"
        f"Статус: {'🟢 включена' if enabled else '🔴 выключена'}\n"
        f"API-ключ: <code>{masked}</code>\n\n"
        "Каналы спонсоров Tgrass показываются в ОП вместе с вашими каналами.\n"
        "Документация: https://tgrass.space/integration"
    )
    return text, _kb(
        ("🔴 Выключить" if enabled else "🟢 Включить", "adm:tg:toggle"),
        ("🔑 Изменить API-ключ", "adm:tg:key"),
        ("🧪 Тест подключения", "adm:tg:test"),
        BACK,
    )


@router.callback_query(F.data == "adm:tg")
async def adm_tgrass(call: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await safe_edit(call, *await _tgrass_screen(session))


@router.callback_query(F.data == "adm:tg:toggle")
async def adm_tg_toggle(call: CallbackQuery, session: AsyncSession) -> None:
    enabled = await crud.get_setting(session, "tgrass_enabled") == "1"
    if not enabled and not await crud.get_setting(session, "tgrass_api_key"):
        await call.answer("Сначала задайте API-ключ", show_alert=True)
        return
    await crud.set_setting(session, "tgrass_enabled", "0" if enabled else "1")
    await call.answer("Готово")
    await safe_edit(call, *await _tgrass_screen(session))


@router.callback_query(F.data == "adm:tg:key")
async def adm_tg_key(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.tg_key)
    await call.answer()
    await safe_edit(call, "🔑 Отправьте новый API-ключ Tgrass (заголовок <code>Auth</code>):", _kb(("❌ Отмена", "adm:tg")))


@router.message(AdminStates.tg_key, F.text)
async def adm_tg_key_input(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await crud.set_setting(session, "tgrass_api_key", message.text.strip())
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer("✅ API-ключ сохранён.")
    await send_screen(message, await _tgrass_screen(session))


@router.callback_query(F.data == "adm:tg:test")
async def adm_tg_test(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    key = await crud.get_setting(session, "tgrass_api_key")
    if not key:
        await call.answer("API-ключ не задан", show_alert=True)
        return
    await call.answer("⏳ Отправляю тестовый запрос…")
    try:
        data = await TgrassService(key).request_offers(user.tg_id, user.username, user.lang, user.is_tg_premium)
        body = json.dumps(data, ensure_ascii=False, indent=2)[:3000]
        text = f"🧪 <b>Ответ Tgrass API</b>\n\n<pre>{html.escape(body)}</pre>"
    except Exception as e:
        text = f"❌ Ошибка подключения: <code>{html.escape(str(e))}</code>"
    await call.message.answer(text, reply_markup=_kb(("🔙 К Tgrass", "adm:tg")))


# ───────────────────────── Рассылка ─────────────────────────


def _parse_buttons(text: str) -> InlineKeyboardMarkup | None:
    kb = InlineKeyboardBuilder()
    count = 0
    for line in text.splitlines():
        if " - " not in line:
            continue
        title, url = line.rsplit(" - ", 1)
        if url.strip().startswith(("http://", "https://", "tg://")):
            kb.button(text=title.strip()[:64], url=url.strip())
            count += 1
    kb.adjust(1)
    return kb.as_markup() if count else None


@router.callback_query(F.data == "adm:bc")
async def adm_bc(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.bc_msg)
    await call.answer()
    await safe_edit(
        call,
        "✉️ <b>РАССЫЛКА</b>\n\nОтправьте сообщение для рассылки: текст с форматированием, фото, видео, "
        "GIF или документ с подписью.",
        _kb(("❌ Отмена", "adm:home")),
    )


@router.message(AdminStates.bc_msg)
async def adm_bc_msg(message: Message, state: FSMContext) -> None:
    await state.update_data(bc_chat=message.chat.id, bc_mid=message.message_id)
    await state.set_state(AdminStates.bc_buttons)
    await message.answer(
        "🔘 Добавить инлайн-кнопки? Отправьте их по одной на строку в формате:\n"
        "<code>Текст кнопки - https://example.com</code>\n\nИли отправьте «<code>-</code>», чтобы без кнопок.",
        reply_markup=_kb(("❌ Отмена", "adm:home")),
    )


@router.message(AdminStates.bc_buttons, F.text)
async def adm_bc_buttons(message: Message, bot: Bot, state: FSMContext) -> None:
    buttons = None if message.text.strip() == "-" else message.text
    if buttons and not _parse_buttons(buttons):
        await message.answer("⚠️ Не удалось распознать кнопки. Формат: <code>Текст - https://…</code> или «-»")
        return
    await state.update_data(bc_buttons=buttons)
    data = await state.get_data()
    await message.answer("👁 <b>Предпросмотр рассылки:</b>")
    await bot.copy_message(message.chat.id, data["bc_chat"], data["bc_mid"], reply_markup=_parse_buttons(buttons or ""))
    async with session_maker() as s:
        total = len(await crud.all_user_ids(s))
    await message.answer(
        f"Получателей: <b>{total}</b>. Начать рассылку?",
        reply_markup=_kb(("🚀 Отправить", "adm:bc:go"), ("❌ Отмена", "adm:home"), width=2),
    )


async def _broadcast(bot: Bot, admin_id: int, from_chat: int, mid: int, buttons: str | None) -> None:
    async with session_maker() as s:
        ids = await crud.all_user_ids(s)
    markup = _parse_buttons(buttons or "")
    sent = blocked = errors = 0
    blocked_ids: list[int] = []
    for i, uid in enumerate(ids, 1):
        for _ in range(3):
            try:
                await bot.copy_message(uid, from_chat, mid, reply_markup=markup)
                sent += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                blocked += 1
                blocked_ids.append(uid)
                break
            except Exception:
                errors += 1
                break
        await asyncio.sleep(0.04)  # ~25 сообщений/сек
        if i % 500 == 0:
            try:
                await bot.send_message(admin_id, f"📨 Прогресс рассылки: {i}/{len(ids)}")
            except Exception:
                pass
    async with session_maker() as s:
        await crud.mark_blocked(s, blocked_ids)
    await bot.send_message(
        admin_id,
        f"✅ <b>Рассылка завершена</b>\n\n📬 Доставлено: <b>{sent}</b>\n🚫 Заблокировали бота: <b>{blocked}</b>\n"
        f"⚠️ Ошибки: <b>{errors}</b>",
    )


@router.callback_query(F.data == "adm:bc:go")
async def adm_bc_go(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    if "bc_mid" not in data:
        await call.answer("Сообщение для рассылки не найдено", show_alert=True)
        return
    await state.clear()
    await call.answer("🚀 Рассылка запущена")
    await safe_edit(call, "🚀 Рассылка запущена в фоне. Отчёт придёт по завершении.", _kb(BACK))
    asyncio.create_task(_broadcast(bot, call.from_user.id, data["bc_chat"], data["bc_mid"], data.get("bc_buttons")))


# ───────────────────────── Пользователи ─────────────────────────


def _user_card(u: User):
    premium = crud.premium_active(u)
    text = (
        f"👤 <b>Пользователь</b>\n{LINE}\n\n"
        f"ID: <code>{u.tg_id}</code>\n"
        f"Юзернейм: {'@' + u.username if u.username else '—'}\n"
        f"Имя: {html.escape(u.first_name or '')}\n"
        f"Регистрация: {u.registered_at:%d.%m.%Y %H:%M}\n"
        f"Капча: {'✅' if u.is_captcha_passed else '❌'}\n"
        f"Premium: {f'💎 до {u.premium_until:%d.%m.%Y %H:%M}' if premium else '❌'}\n"
        f"Поиски: бесплатных {u.free_searches_left}, купленных {u.paid_searches_left}, всего {u.total_searches_done}\n"
        f"Рефералов: {u.referrals_count}\n"
        f"Статус: {'⛔️ ЗАБАНЕН' if u.is_banned else '🟢 активен'}"
    )
    p = f"adm:u:{u.tg_id}"
    kb = InlineKeyboardBuilder()
    kb.button(text="💎 +1 день", callback_data=f"{p}:pd:1")
    kb.button(text="💎 +7 дней", callback_data=f"{p}:pd:7")
    kb.button(text="💎 +30 дней", callback_data=f"{p}:pd:30")
    kb.button(text="💎 Свои дни", callback_data=f"{p}:pdc")
    kb.button(text="❌ Снять Premium", callback_data=f"{p}:pr")
    kb.button(text="🔍 +поиски", callback_data=f"{p}:sr")
    kb.button(text="✅ Разбанить" if u.is_banned else "⛔️ Забанить", callback_data=f"{p}:ban")
    kb.button(text="🔎 Другой пользователь", callback_data="adm:users")
    kb.button(text=BACK[0], callback_data=BACK[1])
    kb.adjust(3, 2, 2, 1, 1)
    return text, kb.as_markup()


@router.callback_query(F.data == "adm:users")
async def adm_users(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.user_search)
    await call.answer()
    await safe_edit(call, "👤 Отправьте <b>Telegram ID</b> или <b>@username</b> пользователя:", _kb(("❌ Отмена", "adm:home")))


@router.message(AdminStates.user_search, F.text)
async def adm_user_find(message: Message, session: AsyncSession, state: FSMContext) -> None:
    u = await crud.find_user(session, message.text)
    if not u:
        await message.answer("❌ Пользователь не найден. Попробуйте ещё раз:", reply_markup=_kb(("❌ Отмена", "adm:home")))
        return
    await state.clear()
    await send_screen(message, _user_card(u))


@router.callback_query(F.data.startswith("adm:u:"))
async def adm_user_action(call: CallbackQuery, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    parts = call.data.split(":")
    u = await crud.get_user(session, int(parts[2]))
    if not u:
        await call.answer("Пользователь не найден", show_alert=True)
        return
    action = parts[3]
    notify = None
    if action == "pd":
        until = await crud.add_premium_days(session, u, int(parts[4]))
        notify = f"🎁 Администратор начислил вам <b>+{parts[4]} дн. Premium</b>!\nДействует до {until:%d.%m.%Y %H:%M} UTC"
    elif action == "pdc":
        await state.set_state(AdminStates.user_days)
        await state.update_data(target=u.tg_id)
        await call.answer()
        await call.message.answer("Сколько дней Premium начислить? (отрицательное число — убавить)")
        return
    elif action == "pr":
        await crud.remove_premium(session, u)
    elif action == "sr":
        await state.set_state(AdminStates.user_searches)
        await state.update_data(target=u.tg_id)
        await call.answer()
        await call.message.answer("Сколько поисков начислить?")
        return
    elif action == "ban":
        u.is_banned = not u.is_banned
        await session.commit()
    if notify:
        try:
            await bot.send_message(u.tg_id, notify)
        except Exception:
            pass
    await call.answer("✅ Готово")
    await safe_edit(call, *_user_card(u))


@router.message(AdminStates.user_searches, F.text)
async def adm_user_add_searches(message: Message, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    if not message.text.strip().lstrip("-").isdigit():
        await message.answer("Введите число:")
        return
    u = await crud.get_user(session, (await state.get_data())["target"])
    n = int(message.text.strip())
    u.paid_searches_left = max(0, u.paid_searches_left + n)
    await session.commit()
    await state.clear()
    if n > 0:
        try:
            await bot.send_message(u.tg_id, f"🎁 Администратор начислил вам <b>+{n} поисков</b>!")
        except Exception:
            pass
    await send_screen(message, _user_card(u))


@router.message(AdminStates.user_days, F.text)
async def adm_user_add_days(message: Message, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    if not message.text.strip().lstrip("-").isdigit():
        await message.answer("Введите число:")
        return
    u = await crud.get_user(session, (await state.get_data())["target"])
    n = int(message.text.strip())
    if n > 0:
        until = await crud.add_premium_days(session, u, n)
        try:
            await bot.send_message(u.tg_id, f"🎁 Администратор начислил вам <b>+{n} дн. Premium</b>!\nДействует до {until:%d.%m.%Y %H:%M} UTC")
        except Exception:
            pass
    elif n < 0 and u.premium_until:
        u.premium_until += timedelta(days=n)
        await session.commit()
    await state.clear()
    await send_screen(message, _user_card(u))


# ───────────────────────── Промокоды ─────────────────────────


async def _promo_screen(session: AsyncSession):
    promos = await crud.list_promocodes(session)
    text = f"🎟 <b>ПРОМОКОДЫ</b>\n{LINE}\n\n"
    kb = InlineKeyboardBuilder()
    if promos:
        for p in promos:
            reward = f"{p.reward_value} дн. Premium" if p.reward_type == "premium_days" else f"{p.reward_value} поисков"
            exp = f", до {p.expires_at:%d.%m.%Y}" if p.expires_at else ""
            text += f"{'🟢' if p.is_active else '🔴'} <code>{p.code}</code> — {reward}, {p.activations_count}/{p.max_activations}{exp}\n"
            if p.is_active:
                kb.button(text=f"🔴 Отключить {p.code}", callback_data=f"adm:pr:off:{p.id}")
    else:
        text += "Промокодов пока нет."
    kb.button(text="➕ Создать промокод", callback_data="adm:pr:new")
    kb.button(text=BACK[0], callback_data=BACK[1])
    kb.adjust(1)
    return text, kb.as_markup()


@router.callback_query(F.data == "adm:promo")
async def adm_promo(call: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await safe_edit(call, *await _promo_screen(session))


@router.callback_query(F.data.startswith("adm:pr:off:"))
async def adm_promo_off(call: CallbackQuery, session: AsyncSession) -> None:
    await crud.deactivate_promocode(session, int(call.data.split(":")[3]))
    await call.answer("Промокод отключён")
    await safe_edit(call, *await _promo_screen(session))


@router.callback_query(F.data == "adm:pr:new")
async def adm_promo_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.promo_new)
    await call.answer()
    await safe_edit(
        call,
        "➕ <b>Новый промокод</b>\n\nОтправьте одной строкой:\n"
        "<code>КОД ТИП КОЛИЧЕСТВО МАКС_АКТИВАЦИЙ [ДНЕЙ_ДЕЙСТВИЯ]</code>\n\n"
        "ТИП: <code>days</code> — дни Premium, <code>searches</code> — поиски\n\n"
        "Примеры:\n<code>START2026 days 3 100</code>\n<code>BONUS20 searches 20 50 7</code>",
        _kb(("❌ Отмена", "adm:promo")),
    )


@router.message(AdminStates.promo_new, F.text)
async def adm_promo_create(message: Message, session: AsyncSession, state: FSMContext) -> None:
    parts = message.text.split()
    types = {"days": "premium_days", "premium_days": "premium_days", "searches": "searches"}
    if (
        len(parts) not in (4, 5)
        or not re.fullmatch(r"[A-Za-z0-9_-]{2,32}", parts[0])
        or parts[1] not in types
        or not all(p.isdigit() for p in parts[2:])
    ):
        await message.answer("⚠️ Неверный формат. Пример: <code>START2026 days 3 100</code>")
        return
    promo = await crud.create_promocode(
        session, parts[0], types[parts[1]], int(parts[2]), int(parts[3]), int(parts[4]) if len(parts) == 5 else None
    )
    if not promo:
        await message.answer("⚠️ Такой промокод уже существует.")
        return
    await state.clear()
    await message.answer(f"✅ Промокод <code>{promo.code}</code> создан.")
    await send_screen(message, await _promo_screen(session))


# ───────────────────────── Настройки ─────────────────────────


async def _settings_screen(session: AsyncSession):
    text = f"⚙️ <b>ГЛОБАЛЬНЫЕ НАСТРОЙКИ</b>\n{LINE}\n\n"
    kb = InlineKeyboardBuilder()
    for key, label in SETTING_LABELS.items():
        text += f"• {label}: <b>{html.escape(await crud.get_setting(session, key))}</b>\n"
        kb.button(text=f"✏️ {label}", callback_data=f"adm:set:{key}")
    battle = await crud.get_setting(session, "battle_enabled") == "1"
    text += f"• Битва никнеймов: <b>{'включена' if battle else 'выключена'}</b>\n"
    kb.button(text=f"⚔️ Битва: {'выключить' if battle else 'включить'}", callback_data="adm:set:battle")
    captcha = await crud.get_setting(session, "captcha_enabled") == "1"
    text += f"• Капча при входе: <b>{'включена' if captcha else 'выключена'}</b>\n"
    kb.button(text=f"🤖 Капча: {'выключить' if captcha else 'включить'}", callback_data="adm:set:captcha")
    kb.button(text=BACK[0], callback_data=BACK[1])
    kb.adjust(1)
    return text, kb.as_markup()


@router.callback_query(F.data == "adm:set")
async def adm_settings(call: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await safe_edit(call, *await _settings_screen(session))


@router.callback_query(F.data == "adm:set:battle")
async def adm_set_battle(call: CallbackQuery, session: AsyncSession) -> None:
    on = await crud.get_setting(session, "battle_enabled") == "1"
    await crud.set_setting(session, "battle_enabled", "0" if on else "1")
    await call.answer("Готово")
    await safe_edit(call, *await _settings_screen(session))


@router.callback_query(F.data == "adm:set:captcha")
async def adm_set_captcha(call: CallbackQuery, session: AsyncSession) -> None:
    on = await crud.get_setting(session, "captcha_enabled") == "1"
    await crud.set_setting(session, "captcha_enabled", "0" if on else "1")
    await call.answer("Готово")
    await safe_edit(call, *await _settings_screen(session))


@router.callback_query(F.data.startswith("adm:set:"))
async def adm_set_key(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":", 2)[2]
    if key not in SETTING_LABELS:
        await call.answer()
        return
    await state.set_state(AdminStates.set_value)
    await state.update_data(set_key=key)
    await call.answer()
    await safe_edit(call, f"✏️ Отправьте новое значение для «{SETTING_LABELS[key]}»:", _kb(("❌ Отмена", "adm:set")))


@router.message(AdminStates.set_value, F.text)
async def adm_set_value(message: Message, session: AsyncSession, state: FSMContext) -> None:
    key = (await state.get_data())["set_key"]
    value = message.text.strip()
    if key != "support_url" and not value.isdigit():
        await message.answer("⚠️ Нужно целое неотрицательное число:")
        return
    if key == "support_url" and not value.startswith(("http://", "https://", "tg://")):
        await message.answer("⚠️ Ссылка должна начинаться с https://")
        return
    await crud.set_setting(session, key, value)
    await state.clear()
    await message.answer("✅ Сохранено.")
    await send_screen(message, await _settings_screen(session))
