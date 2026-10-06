"""Админка: премиум-эмодзи и цветные кнопки."""

import json
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import crud
from handlers.sections import safe_edit
from services.ui_style import BUNDLED_EMOJI, UI_EMOJI, VS16

router = Router(name="admin_style")
router.message.filter(F.from_user.id.in_(settings.admin_ids))
router.callback_query.filter(F.from_user.id.in_(settings.admin_ids))


class StyleStates(StatesGroup):
    emoji = State()


async def _get_map(session: AsyncSession) -> dict[str, str]:
    try:
        return json.loads(await crud.get_setting(session, "premium_emoji") or "{}")
    except ValueError:
        return {}


async def _style_screen(session: AsyncSession):
    mapping = await _get_map(session)
    emoji_on = await crud.get_setting(session, "premium_emoji_enabled") == "1"
    colors_on = await crud.get_setting(session, "button_colors_enabled") == "1"
    known = {k.replace(VS16, "") for k in [*BUNDLED_EMOJI, *mapping]}
    missing = [e for e in UI_EMOJI if e not in known]

    text = (
        f"✨ <b>ОФОРМЛЕНИЕ</b>\n\n"
        f"🎨 Цветные кнопки: <b>{'включены' if colors_on else 'выключены'}</b>\n"
        f"💎 Премиум-эмодзи: <b>{'включены' if emoji_on else 'выключены'}</b>\n"
        f"├ встроенный анимированный пак Telegram: {len(BUNDLED_EMOJI)} шт\n"
        f"└ добавлено вами (важнее встроенных): {len(mapping)} шт\n\n"
    )
    if mapping:
        preview = " ".join(f'<tg-emoji emoji-id="{v}">{k}</tg-emoji>' for k, v in list(mapping.items())[:40])
        text += f"Ваши эмодзи: {preview}\n\n"
    if missing:
        text += "Без премиум-версии: " + " ".join(missing) + "\n\n"
    text += (
        "<i>Премиум-эмодзи в сообщениях бота работают, только если у владельца бота есть "
        "Telegram Premium. Иначе бот автоматически отправит обычные эмодзи</i>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить премиум-эмодзи", callback_data="adm:st:add")
    kb.button(text=f"🎨 Цвета: {'выключить' if colors_on else 'включить'}", callback_data="adm:st:colors")
    kb.button(text=f"💎 Эмодзи: {'выключить' if emoji_on else 'включить'}", callback_data="adm:st:emoji")
    if mapping:
        kb.button(text="🗑 Удалить мои эмодзи", callback_data="adm:st:clear")
    kb.button(text="🔙 В админку", callback_data="adm:home")
    kb.adjust(1)
    return text, kb.as_markup()


@router.callback_query(F.data == "adm:style")
async def style_menu(call: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await safe_edit(call, *await _style_screen(session))


@router.callback_query(F.data.in_({"adm:st:colors", "adm:st:emoji"}))
async def style_toggle(call: CallbackQuery, session: AsyncSession) -> None:
    key = "button_colors_enabled" if call.data == "adm:st:colors" else "premium_emoji_enabled"
    on = await crud.get_setting(session, key) == "1"
    await crud.set_setting(session, key, "0" if on else "1")
    await call.answer("Готово")
    await safe_edit(call, *await _style_screen(session))


@router.callback_query(F.data == "adm:st:clear")
async def style_clear(call: CallbackQuery, session: AsyncSession) -> None:
    await crud.set_setting(session, "premium_emoji", "{}")
    await call.answer("Очищено")
    await safe_edit(call, *await _style_screen(session))


@router.callback_query(F.data == "adm:st:add")
async def style_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(StyleStates.emoji)
    await call.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data="adm:style")
    await safe_edit(
        call,
        "➕ <b>Добавление премиум-эмодзи</b>\n\n"
        "Отправьте сообщение с премиум-эмодзи (из любых паков, можно сразу много). "
        "Бот запомнит, какой обычный эмодзи каждый из них заменяет, — например, премиум 🔍 "
        "будет показываться везде вместо обычного 🔍.\n\n"
        "Также можно вручную: <code>🔍 5368324170671202286</code> (по строке на эмодзи)\n\n"
        "Можно отправить несколько сообщений подряд, затем нажмите «Готово»",
        kb.as_markup(),
    )


@router.message(StyleStates.emoji)
async def style_add_input(message: Message, session: AsyncSession) -> None:
    mapping = await _get_map(session)
    added = []
    text = message.text or message.caption or ""
    for ent in (message.entities or message.caption_entities or []):
        if ent.type == "custom_emoji" and ent.custom_emoji_id:
            alt = ent.extract_from(text)
            if alt:
                mapping[alt] = ent.custom_emoji_id
                added.append(f'<tg-emoji emoji-id="{ent.custom_emoji_id}">{alt}</tg-emoji>')
    if message.sticker and message.sticker.custom_emoji_id and message.sticker.emoji:
        mapping[message.sticker.emoji] = message.sticker.custom_emoji_id
        added.append(message.sticker.emoji)
    if not added:
        for line in text.splitlines():
            m = re.fullmatch(r"\s*(\S+)\s+(\d{5,25})\s*", line)
            if m:
                mapping[m.group(1)] = m.group(2)
                added.append(f'<tg-emoji emoji-id="{m.group(2)}">{m.group(1)}</tg-emoji>')
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data="adm:style")
    if not added:
        await message.answer(
            "⚠️ Премиум-эмодзи не найдены. Отправьте именно премиум-эмодзи (не обычные) "
            "или строку вида <code>🔍 5368324170671202286</code>",
            reply_markup=kb.as_markup(),
        )
        return
    await crud.set_setting(session, "premium_emoji", json.dumps(mapping, ensure_ascii=False))
    await message.answer(f"✅ Добавлено: {' '.join(added)}\n\nВсего настроено: {len(mapping)}", reply_markup=kb.as_markup())
