import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from database import crud
from database.models import User, utcnow
from handlers.sections import build_search, cooldown_left, safe_edit
from keyboards import inline
from services.username_checker import (
    STATUS_TEXT,
    UsernameChecker,
    generate_from_mask,
    generate_nice,
    is_valid_username,
    normalize,
    validate_mask,
)
from texts import FOUND_TEXT, MASK_PROMPT, PREMIUM_ONLY_TEXT, TRAP_PROMPT, LINE

log = logging.getLogger(__name__)
router = Router(name="search")

MAX_TRAPS = 10
_in_progress: set[int] = set()


class SearchStates(StatesGroup):
    mask = State()
    trap = State()


@router.callback_query(F.data == "menu:search")
async def open_search(call: CallbackQuery, bot: Bot, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.set_state(None)
    await call.answer()
    await safe_edit(call, *await build_search(session, user, bot))


def _no_balance_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐️ Premium", callback_data="shop:premium")
    kb.button(text="📦 Пакеты поисков", callback_data="shop:packs")
    kb.button(text="🎁 Ежедневный бонус", callback_data="prof:bonus")
    kb.button(text="👥 Пригласить друзей", callback_data="menu:ref")
    kb.button(text="🔙 Назад в поиск", callback_data="menu:search")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


async def _reserve_search(event: CallbackQuery | Message, session: AsyncSession, user: User) -> str | None:
    """Списывает поиск. Возвращает источник списания ('premium' | 'free' | 'paid') или None, если нельзя."""
    if crud.premium_active(user):
        return "premium"
    cooldown = await crud.get_setting_int(session, "search_cooldown_sec")
    left = cooldown_left(user, cooldown)
    if left:
        msg = f"⏱ Подождите {left} сек. перед следующим поиском.\n\n💎 В Premium задержки нет!"
        if isinstance(event, CallbackQuery):
            await event.answer(msg, show_alert=True)
        else:
            await event.answer(msg)
        return None
    if user.free_searches_left > 0:
        user.free_searches_left -= 1
        source = "free"
    elif user.paid_searches_left > 0:
        user.paid_searches_left -= 1
        source = "paid"
    else:
        if isinstance(event, CallbackQuery):
            await event.answer()
        await safe_edit(
            event,
            "😔 <b>Поиски на сегодня закончились</b>\n\n"
            "Бесплатные поиски обновляются каждый день в 00:00 UTC.\n"
            "Чтобы продолжить прямо сейчас — оформите Premium, купите пакет поисков, "
            "заберите ежедневный бонус или пригласите друзей.",
            _no_balance_kb(),
        )
        return None
    user.last_search_at = utcnow()
    await session.commit()
    return source


async def _refund(session: AsyncSession, user: User, source: str) -> None:
    if source == "free":
        user.free_searches_left += 1
    elif source == "paid":
        user.paid_searches_left += 1
    user.last_search_at = None
    await session.commit()


async def run_search(
    event: CallbackQuery | Message,
    session: AsyncSession,
    user: User,
    checker: UsernameChecker,
    mode: str,
    generator,
    title: str,
    attempts: int = 40,
) -> None:
    if user.tg_id in _in_progress:
        if isinstance(event, CallbackQuery):
            await event.answer("⏳ Поиск уже идёт, подождите…", show_alert=True)
        return
    # Ставим блокировку до списания, чтобы двойной клик не запустил два поиска
    _in_progress.add(user.tg_id)
    try:
        source = await _reserve_search(event, session, user)
        if not source:
            return
        wait_text = f"⏳ <b>{title}</b>\n\nГенерирую варианты и проверяю их в Telegram и на Fragment…"
        if isinstance(event, CallbackQuery):
            await event.answer()
        if isinstance(event, CallbackQuery) and isinstance(event.message, Message):
            msg = event.message
            await safe_edit(event, wait_text)
        else:
            target = event if isinstance(event, Message) else event.message
            msg = await event.bot.send_message(target.chat.id, wait_text)

        try:
            exclude = await crud.recently_checked_by_user(session, user.tg_id, since_hours=24 * 7)
            result, checked = await checker.find_free(generator, attempts=attempts, exclude=exclude)
        except Exception:
            log.exception("Ошибка поиска")
            await _refund(session, user, source)
            await msg.edit_text("⚠️ Сервис проверки временно недоступен. Поиск <b>не списан</b> — попробуйте позже.",
                                reply_markup=inline.retry_kb(mode))
            return

        if not result:
            await _refund(session, user, source)
            await msg.edit_text(
                f"😔 Проверено {checked} вариантов, но все заняты.\n\n"
                "Поиск <b>не списан</b> — попробуйте ещё раз или измените маску.",
                reply_markup=inline.retry_kb(mode),
            )
            return

        row = await crud.add_search(session, user.tg_id, result.username, True, True, "free")
        user.total_searches_done += 1
        await session.commit()
        text = FOUND_TEXT.format(username=result.username, length=len(result.username))
        text += f"\n\n<i>🔎 Проверено вариантов: {checked}</i>"
        await msg.edit_text(text, reply_markup=inline.found_kb(result.username, mode, row.id), disable_web_page_preview=True)
    finally:
        _in_progress.discard(user.tg_id)


async def _premium_gate(call: CallbackQuery, user: User) -> bool:
    if crud.premium_active(user):
        return True
    await call.answer()
    await safe_edit(call, PREMIUM_ONLY_TEXT, inline.premium_only_kb())
    return False


@router.callback_query(F.data == "s:5")
async def search_5(call: CallbackQuery, session: AsyncSession, user: User, checker: UsernameChecker) -> None:
    if await _premium_gate(call, user):
        await run_search(call, session, user, checker, "5", lambda: generate_nice(5), "Ищу редкий 5-буквенный юзернейм", 300)


@router.callback_query(F.data == "s:6")
async def search_6(call: CallbackQuery, session: AsyncSession, user: User, checker: UsernameChecker) -> None:
    await run_search(call, session, user, checker, "6", lambda: generate_nice(6), "Ищу 6-буквенный юзернейм", 40)


@router.callback_query(F.data == "s:mask")
async def search_mask_prompt(call: CallbackQuery, user: User, state: FSMContext) -> None:
    if not await _premium_gate(call, user):
        return
    await call.answer()
    await state.set_state(SearchStates.mask)
    await safe_edit(call, MASK_PROMPT, inline.cancel_kb())


@router.message(SearchStates.mask, F.text)
async def search_mask_input(message: Message, session: AsyncSession, user: User, state: FSMContext, checker: UsernameChecker) -> None:
    mask = message.text.strip().lstrip("@")
    error = validate_mask(mask)
    if error:
        await message.answer(f"⚠️ {error}\n\nОтправьте другую маску:", reply_markup=inline.cancel_kb())
        return
    await state.set_state(None)
    await state.update_data(last_mask=mask)
    await run_search(message, session, user, checker, "m", lambda: generate_from_mask(mask), f"Ищу по маске {mask}", 150)


@router.callback_query(F.data == "s:m")
async def search_mask_again(call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext, checker: UsernameChecker) -> None:
    if not await _premium_gate(call, user):
        return
    mask = (await state.get_data()).get("last_mask")
    if not mask:
        await search_mask_prompt(call, user, state)
        return
    await run_search(call, session, user, checker, "m", lambda: generate_from_mask(mask), f"Ищу по маске {mask}", 150)


@router.callback_query(F.data.startswith("s:save:"))
async def save_finding(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    ok = await crud.save_finding(session, user.tg_id, int(call.data.split(":")[2]))
    await call.answer("📁 Сохранено в «Мои находки»" if ok else "Не удалось сохранить", show_alert=not ok)


# ───────────────────────── Ловушка на ник ─────────────────────────


async def _traps_screen(session: AsyncSession, user: User):
    traps = await crud.get_user_traps(session, user.tg_id)
    text = f"🪤 <b>ЛОВУШКА НА НИК (СНАЙПЕР)</b>\n{LINE}\n\n"
    if traps:
        text += "Активные ловушки:\n"
        for t in traps:
            checked = f"{t.last_checked_at:%d.%m %H:%M}" if t.last_checked_at else "ожидает"
            text += f"• <b>@{t.target_username}</b> — последняя проверка: {checked}\n"
        text += "\nНажмите на ник, чтобы удалить ловушку."
    else:
        text += "У вас пока нет активных ловушек.\n\nДобавьте занятый ник — бот сообщит, как только он освободится."
    text += f"\n\nЛимит: {len(traps)}/{MAX_TRAPS}"
    return text, inline.traps_kb(traps)


@router.callback_query(F.data == "s:trap")
async def trap_menu(call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext) -> None:
    if not await _premium_gate(call, user):
        return
    await state.set_state(None)
    await call.answer()
    await safe_edit(call, *await _traps_screen(session, user))


@router.callback_query(F.data == "trap:add")
async def trap_add(call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext) -> None:
    if not await _premium_gate(call, user):
        return
    if len(await crud.get_user_traps(session, user.tg_id)) >= MAX_TRAPS:
        await call.answer(f"Достигнут лимит в {MAX_TRAPS} ловушек", show_alert=True)
        return
    await call.answer()
    await state.set_state(SearchStates.trap)
    await safe_edit(call, TRAP_PROMPT, inline.cancel_kb("s:trap"))


@router.message(SearchStates.trap, F.text)
async def trap_input(message: Message, session: AsyncSession, user: User, state: FSMContext, checker: UsernameChecker) -> None:
    name = normalize(message.text)
    if not is_valid_username(name):
        await message.answer(
            "⚠️ Некорректный юзернейм. Допустимы латиница, цифры и «_», длина 5–32, начинается с буквы.",
            reply_markup=inline.cancel_kb("s:trap"),
        )
        return
    wait = await message.answer(f"⏳ Проверяю @{name}…")
    res = await checker.check(name)
    if res.is_free:
        await state.set_state(None)
        kb = InlineKeyboardBuilder()
        kb.button(text="🚀 Занять никнейм", url=f"https://t.me/{name}")
        kb.button(text="🔙 К ловушкам", callback_data="s:trap")
        kb.adjust(1)
        await wait.edit_text(f"🎉 <b>@{name}</b> свободен прямо сейчас! Ловушка не нужна — занимайте:", reply_markup=kb.as_markup())
        return
    if res.status == "invalid":
        await wait.edit_text("⛔️ Этот юзернейм недопустим в Telegram. Отправьте другой:", reply_markup=inline.cancel_kb("s:trap"))
        return

    trap = await crud.add_trap(session, user.tg_id, name)
    await state.set_state(None)
    status = STATUS_TEXT.get(res.status, res.status)
    if res.fragment_price:
        status += f" ({res.fragment_price} TON)"
    if trap:
        text = (
            f"✅ Ловушка на <b>@{name}</b> установлена!\n\n"
            f"Текущий статус: {status}\n\n"
            "Как только ник освободится — вы получите мгновенное уведомление 🚨"
        )
    else:
        text = f"ℹ️ Ловушка на <b>@{name}</b> уже активна."
    await wait.edit_text(text, reply_markup=inline.back_kb("s:trap", "🪤 Мои ловушки"))


@router.callback_query(F.data.startswith("trap:del:"))
async def trap_delete(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    await crud.delete_trap(session, user.tg_id, int(call.data.split(":")[2]))
    await call.answer("🗑 Ловушка удалена")
    await safe_edit(call, *await _traps_screen(session, user))
