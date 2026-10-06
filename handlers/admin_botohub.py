"""Админка: интеграция BotoHub (спонсоры для бонуса за подписку)."""

import html
import json

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import crud
from database.models import User
from handlers.sections import safe_edit
from services.botohub_service import BotohubService

router = Router(name="admin_botohub")
router.message.filter(F.from_user.id.in_(settings.admin_ids))
router.callback_query.filter(F.from_user.id.in_(settings.admin_ids))


class BotohubStates(StatesGroup):
    key = State()


def _kb(*rows: tuple[str, str]):
    kb = InlineKeyboardBuilder()
    for text, data in rows:
        kb.button(text=text, callback_data=data)
    kb.adjust(1)
    return kb.as_markup()


async def _screen(session: AsyncSession):
    enabled = await crud.get_setting(session, "botohub_enabled") == "1"
    key = await crud.get_setting(session, "botohub_api_key")
    masked = f"{key[:4]}…{key[-4:]}" if len(key) > 10 else ("задан" if key else "не задан")
    text = (
        "🤖 <b>ИНТЕГРАЦИЯ BOTOHUB</b>\n\n"
        f"Статус: {'🟢 включена' if enabled else '🔴 выключена'}\n"
        f"API-ключ: <code>{masked}</code>\n\n"
        "Спонсоры BotoHub — за бонусные поиски (экран «+N поисков за подписку»), вместе с Tgrass. "
        "Ваши каналы из «📢 Спонсоры» — отдельно, обязательная подписка\n"
        "Документация: https://botohub.me/integration"
    )
    return text, _kb(
        ("🔴 Выключить" if enabled else "🟢 Включить", "adm:bh:toggle"),
        ("🔑 Изменить API-ключ", "adm:bh:key"),
        ("🧪 Тест подключения", "adm:bh:test"),
        ("🔙 В админку", "adm:home"),
    )


@router.callback_query(F.data == "adm:bh")
async def bh_menu(call: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await safe_edit(call, *await _screen(session))


@router.callback_query(F.data == "adm:bh:toggle")
async def bh_toggle(call: CallbackQuery, session: AsyncSession) -> None:
    enabled = await crud.get_setting(session, "botohub_enabled") == "1"
    if not enabled and not await crud.get_setting(session, "botohub_api_key"):
        await call.answer("Сначала задайте API-ключ", show_alert=True)
        return
    await crud.set_setting(session, "botohub_enabled", "0" if enabled else "1")
    await call.answer("Готово")
    await safe_edit(call, *await _screen(session))


@router.callback_query(F.data == "adm:bh:key")
async def bh_key(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BotohubStates.key)
    await call.answer()
    await safe_edit(call, "🔑 Отправьте API-токен BotoHub (заголовок <code>Auth</code>):", _kb(("❌ Отмена", "adm:bh")))


@router.message(BotohubStates.key, F.text)
async def bh_key_input(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await crud.set_setting(session, "botohub_api_key", message.text.strip())
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    text, kb = await _screen(session)
    await message.answer("✅ API-ключ сохранён\n\n" + text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data == "adm:bh:test")
async def bh_test(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    key = await crud.get_setting(session, "botohub_api_key")
    if not key:
        await call.answer("API-ключ не задан", show_alert=True)
        return
    await call.answer("⏳ Отправляю тестовый запрос…")
    try:
        data = await BotohubService(key).request_tasks(user.tg_id)
        body = json.dumps(data, ensure_ascii=False, indent=2)[:3000]
        text = f"🧪 <b>Ответ BotoHub API</b>\n\n<pre>{html.escape(body)}</pre>"
    except Exception as e:
        text = f"❌ Ошибка подключения: <code>{html.escape(str(e))}</code>"
    await call.message.answer(text, reply_markup=_kb(("🔙 К BotoHub", "adm:bh")))
