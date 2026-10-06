import asyncio
import logging
import time
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import crud
from database.models import User, msk, utcnow
from handlers.sections import build_search, cooldown_left, premium_limit_phrase, safe_edit
from keyboards import inline
from services.free_pool import FreeNamePool, activity
from services import social_checker
from services.nickname_sniper import trap_networks
from services.op_manager import check_sponsors
from services.username_checker import (
    STATUS_TEXT,
    CheckerUnavailable,
    UsernameChecker,
    generate_from_mask,
    generate_nice,
    is_valid_username,
    normalize,
    validate_mask,
    validate_word,
    word_variants,
)
from texts import (
    FOUND_TEXT,
    MASK_PROMPT,
    NO_SPONSORS_TEXT,
    PAYWALL_TEXT,
    PREMIUM_ONLY_TEXT,
    SPONSOR_BONUS_TEXT,
    TRAP_PROMPT,
    WORD_PROMPT,
)

log = logging.getLogger(__name__)
router = Router(name="search")

MAX_TRAPS = 10  # у админов (ADMIN_IDS) ловушек без лимита
LIKELY_NOTE = (
    "\n\n🟡 <i>Вероятно свободен: Telegram сейчас ограничил точную проверку, ник проверен по t.me "
    "и Fragment. Обычно он свободен, но изредка оказывается зарезервирован</i>"
)
TRAPS_SHOWN = 50  # больше не выводим списком: лимиты Telegram на длину сообщения и число кнопок
_in_progress: set[int] = set()


class SearchStates(StatesGroup):
    mask = State()
    trap = State()
    word = State()


@router.callback_query(F.data == "menu:search")
async def open_search(call: CallbackQuery, bot: Bot, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.set_state(None)
    await call.answer()
    await safe_edit(call, *await build_search(session, user, bot))


def _paywall_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐️ Купить Premium", callback_data="shop:premium")
    kb.button(text="📦 Пакеты поисков", callback_data="shop:packs")
    kb.button(text="👥 Premium бесплатно за друзей", callback_data="menu:ref")
    kb.button(text="🔙 Назад в поиск", callback_data="menu:search")
    kb.adjust(1)
    return kb.as_markup()


async def show_no_balance(event: CallbackQuery | Message, session: AsyncSession, user: User) -> None:
    """Поиски закончились: сначала предлагаем бонус за подписку на спонсоров, потом — оплату."""
    if not user.sponsor_bonus_claimed:
        bonus = await crud.get_setting_int(session, "sponsor_bonus")
        state = await check_sponsors(event.bot, session, user)
        if state.available:
            await safe_edit(event, SPONSOR_BONUS_TEXT.format(bonus=bonus), inline.sponsor_bonus_kb(state.missing))
            return
        # Спонсоров сейчас нет — бонус не выдаём даром, предлагаем зайти позже или купить
        if isinstance(event, CallbackQuery) and event.data == "s:bonus":
            await safe_edit(event, NO_SPONSORS_TEXT.format(bonus=bonus), _paywall_kb())
            return
    await safe_edit(event, PAYWALL_TEXT.format(limit=await premium_limit_phrase(session)), _paywall_kb())


@router.callback_query(F.data == "s:bonus")
async def sponsor_bonus(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    if user.sponsor_bonus_claimed:
        await call.answer("Бонус за подписку уже получен 👌", show_alert=True)
        return
    await call.answer()
    await show_no_balance(call, session, user)


async def _reserve_search(event: CallbackQuery | Message, session: AsyncSession, user: User) -> str | None:
    """Списывает поиск. Возвращает источник списания ('premium' | 'free' | 'paid') или None, если нельзя."""
    if crud.premium_active(user):
        if await premium_limit_reached(event, session, user):
            return None
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
        await show_no_balance(event, session, user)
        return None
    user.last_search_at = utcnow()
    await session.commit()
    return source


def _until_msk_midnight() -> str:
    left = crud.msk_day_start() + timedelta(days=1) - utcnow()
    hours, minutes = divmod(max(60, int(left.total_seconds())) // 60, 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


async def premium_limit_reached(event: CallbackQuery | Message, session: AsyncSession, user: User) -> bool:
    """Дневной лимит Premium исчерпан — сообщает об этом и возвращает True."""
    daily = await crud.premium_daily_left(session, user)
    if daily is None or daily[0] > 0:
        return False
    text = (f"💎 Дневной лимит Premium исчерпан: {daily[1]} юзернеймов в сутки.\n\n"
            f"Новые поиски — с 00:00 МСК (через {_until_msk_midnight()}).")
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)
    return True


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
    name_pool: FreeNamePool | None = None,
    pool_length: int | None = None,
) -> None:
    await _guarded(event, user, _run_search(event, session, user, checker, mode, generator, title, name_pool, pool_length))


async def _guarded(event: CallbackQuery | Message, user: User, search) -> None:
    """Один поиск на пользователя за раз; фоновый поиск запаса ников на это время встаёт на паузу."""
    if user.tg_id in _in_progress:
        search.close()
        if isinstance(event, CallbackQuery):
            await event.answer("⏳ Поиск уже идёт, подождите…", show_alert=True)
        return
    # Ставим блокировку до списания, чтобы двойной клик не запустил два поиска
    _in_progress.add(user.tg_id)
    try:
        with activity.user_search():
            await search
    finally:
        _in_progress.discard(user.tg_id)


async def _wait_message(event: CallbackQuery | Message, wait_text: str) -> Message:
    """Показывает «ищу…» в сообщении с кнопкой или новым сообщением."""
    if isinstance(event, CallbackQuery):
        await event.answer()
    if isinstance(event, CallbackQuery) and isinstance(event.message, Message):
        await safe_edit(event, wait_text)
        return event.message
    target = event if isinstance(event, Message) else event.message
    return await event.bot.send_message(target.chat.id, wait_text)


async def _run_search(
    event: CallbackQuery | Message,
    session: AsyncSession,
    user: User,
    checker: UsernameChecker,
    mode: str,
    generator,
    title: str,
    name_pool: FreeNamePool | None,
    pool_length: int | None,
) -> None:
    source = await _reserve_search(event, session, user)
    if not source:
        return
    wait_text = f"⏳ <b>{title}</b>\n\nГенерирую варианты и проверяю их в Telegram и на Fragment…"
    msg = await _wait_message(event, wait_text)

    ticker = asyncio.create_task(_tick(msg, wait_text))
    try:
        exclude = await crud.recently_checked_by_user(session, user.tg_id, since_hours=24 * 7)
        result = None
        if name_pool and pool_length:
            result = await name_pool.take(pool_length, exclude)
        if not result:
            result, _ = await checker.find_free(generator, exclude=exclude)
    except CheckerUnavailable:
        await _refund(session, user, source)
        await msg.edit_text(
            "⚠️ Сервисы проверки (Telegram / Fragment) сейчас не отвечают.\n\n"
            "Поиск <b>не списан</b> — попробуйте через пару минут.",
            reply_markup=inline.retry_kb(mode),
        )
        return
    except Exception:
        log.exception("Ошибка поиска")
        await _refund(session, user, source)
        await msg.edit_text("⚠️ Сервис проверки временно недоступен. Поиск <b>не списан</b> — попробуйте позже.",
                            reply_markup=inline.retry_kb(mode))
        return
    finally:
        ticker.cancel()

    if not result:
        await _refund(session, user, source)
        await msg.edit_text(
            "😔 Свободных вариантов не нашлось — все комбинации заняты.\n\n"
            "Поиск <b>не списан</b> — попробуйте другую маску.",
            reply_markup=inline.retry_kb(mode),
        )
        return

    row = await crud.add_search(session, user.tg_id, result.username, True, True, "free")
    user.total_searches_done += 1
    await session.commit()
    text = FOUND_TEXT.format(username=result.username, length=len(result.username))
    if result.is_likely:
        text += LIKELY_NOTE
    await msg.edit_text(text, reply_markup=inline.found_kb(result.username, mode, row.id), disable_web_page_preview=True)


async def _tick(msg: Message, wait_text: str) -> None:
    """Обновляет таймер в сообщении поиска, чтобы было видно, что бот работает."""
    started = time.monotonic()
    try:
        while True:
            await asyncio.sleep(10)  # реже правим сообщение — меньше запросов к Telegram
            elapsed = int(time.monotonic() - started)
            try:
                await msg.edit_text(f"{wait_text}\n\n⏱ Идёт поиск: {elapsed} сек")
            except Exception:
                pass
    except asyncio.CancelledError:
        pass


async def _premium_gate(call: CallbackQuery, user: User) -> bool:
    if crud.premium_active(user):
        return True
    await call.answer()
    await safe_edit(call, PREMIUM_ONLY_TEXT, inline.premium_only_kb())
    return False


@router.callback_query(F.data == "s:5")
async def search_5(
    call: CallbackQuery, session: AsyncSession, user: User, checker: UsernameChecker, name_pool: FreeNamePool
) -> None:
    if await _premium_gate(call, user):
        await run_search(call, session, user, checker, "5", lambda: generate_nice(5),
                         "Ищу редкий 5-буквенный юзернейм", name_pool, 5)


@router.callback_query(F.data == "s:6")
async def search_6(
    call: CallbackQuery, session: AsyncSession, user: User, checker: UsernameChecker, name_pool: FreeNamePool
) -> None:
    await run_search(call, session, user, checker, "6", lambda: generate_nice(6),
                     "Ищу 6-буквенный юзернейм", name_pool, 6)


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
    await run_search(message, session, user, checker, "m", lambda: generate_from_mask(mask), f"Ищу по маске {mask}")


@router.callback_query(F.data == "s:m")
async def search_mask_again(call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext, checker: UsernameChecker) -> None:
    if not await _premium_gate(call, user):
        return
    mask = (await state.get_data()).get("last_mask")
    if not mask:
        await search_mask_prompt(call, user, state)
        return
    await run_search(call, session, user, checker, "m", lambda: generate_from_mask(mask), f"Ищу по маске {mask}")


@router.callback_query(F.data.startswith("s:save:"))
async def save_finding(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    ok = await crud.save_finding(session, user.tg_id, int(call.data.split(":")[2]))
    await call.answer("📁 Сохранено в «Мои находки»" if ok else "Не удалось сохранить", show_alert=not ok)


# ───────────────────────── Поиск по слову ─────────────────────────

WORD_LIMIT = 5  # свободных вариантов за один поиск


@router.callback_query(F.data == "s:word")
async def search_word_prompt(call: CallbackQuery, user: User, state: FSMContext) -> None:
    if not await _premium_gate(call, user):
        return
    await call.answer()
    await state.set_state(SearchStates.word)
    await safe_edit(call, WORD_PROMPT, inline.cancel_kb())


@router.message(SearchStates.word, F.text)
async def search_word_input(
    message: Message, session: AsyncSession, user: User, state: FSMContext, checker: UsernameChecker
) -> None:
    word = message.text.strip().lstrip("@").lower()
    error = validate_word(word)
    if error:
        await message.answer(f"⚠️ {error}\n\nОтправьте другое слово:", reply_markup=inline.cancel_kb())
        return
    await state.set_state(None)
    await state.update_data(word=word, word_pos=0, word_ids=[], word_trap=False)
    await _guarded(message, user, _run_word_search(message, session, user, state, checker, word, 0))


@router.callback_query(F.data == "s:wmore")
async def search_word_more(
    call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext, checker: UsernameChecker
) -> None:
    if not await _premium_gate(call, user):
        return
    data = await state.get_data()
    word = data.get("word")
    if not word:
        await search_word_prompt(call, user, state)
        return
    await _guarded(call, user, _run_word_search(call, session, user, state, checker, word, data.get("word_pos", 0)))


async def _run_word_search(
    event: CallbackQuery | Message,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    checker: UsernameChecker,
    word: str,
    pos: int,
) -> None:
    source = await _reserve_search(event, session, user)
    if not source:
        return
    wait_text = f"⏳ <b>Подбираю ники по слову «{word}»</b>\n\nПроверяю варианты в Telegram и на Fragment…"
    msg = await _wait_message(event, wait_text)
    ticker = asyncio.create_task(_tick(msg, wait_text))

    names = word_variants(word)
    exact = None
    try:
        exclude = await crud.recently_checked_by_user(session, user.tg_id, since_hours=24 * 7)
        found = []
        # Само слово проверяем только на первой странице
        if pos == 0 and is_valid_username(word):
            exact = await checker.check(word)
            if exact.is_free and word not in exclude:
                found.append(exact)
        todo = [(i, n) for i, n in enumerate(names) if i >= pos and n not in exclude]
        # Не больше, чем осталось в дневном лимите Premium
        daily = await crud.premium_daily_left(session, user)
        need = min(WORD_LIMIT, daily[0]) - len(found) if daily else WORD_LIMIT - len(found)
        more, done = await checker.find_many([n for _, n in todo], need) if need > 0 else ([], 0)
        found += more
        new_pos = todo[done - 1][0] + 1 if done else pos
        if done == len(todo):
            new_pos = len(names)
    except CheckerUnavailable:
        await _refund(session, user, source)
        await msg.edit_text(
            "⚠️ Сервисы проверки (Telegram / Fragment) сейчас не отвечают.\n\n"
            "Поиск <b>не списан</b> — попробуйте через пару минут.",
            reply_markup=inline.retry_kb("word"),
        )
        return
    except Exception:
        log.exception("Ошибка поиска по слову")
        await _refund(session, user, source)
        await msg.edit_text("⚠️ Сервис проверки временно недоступен. Поиск <b>не списан</b> — попробуйте позже.",
                            reply_markup=inline.retry_kb("word"))
        return
    finally:
        ticker.cancel()

    data = await state.get_data()
    can_trap = data.get("word_trap", False)
    if exact is not None:
        can_trap = not exact.is_free and exact.status != "invalid"
    exact_line = ""
    if exact is not None and not exact.is_free and exact.status != "invalid":
        status = STATUS_TEXT.get(exact.status, exact.status)
        if exact.fragment_price:
            status += f" ({exact.fragment_price} TON)"
        exact_line = f"\n\n<b>@{word}</b> — {status}"

    if not found:
        await _refund(session, user, source)
        await state.update_data(word_pos=len(names), word_ids=[], word_trap=can_trap)
        await msg.edit_text(
            f"😔 Свободных вариантов для «{word}» больше не нашлось.{exact_line}\n\n"
            "Поиск <b>не списан</b> — попробуйте другое слово.",
            reply_markup=inline.word_found_kb(word, False, can_trap, can_save=False),
        )
        return

    ids = []
    for r in found:
        row = await crud.add_search(session, user.tg_id, r.username, True, True, "free")
        ids.append(row.id)
    user.total_searches_done += 1
    await session.commit()
    await state.update_data(word_pos=new_pos, word_ids=ids, word_trap=can_trap)

    lines = [f"{i}. <code>@{r.username}</code>{' 🟡' if r.is_likely else ''}" for i, r in enumerate(found, 1)]
    head = "🎉 Само слово свободно!\n\n" if exact is not None and exact in found else ""
    text = (
        f"✍️ <b>Свободные ники по слову «{word}»</b>\n\n{head}"
        + "\n".join(lines)
        + exact_line
        + "\n\n👆 Нажмите на ник, чтобы скопировать. Занимайте скорее — свободные ники быстро разбирают"
        + (LIKELY_NOTE if any(r.is_likely for r in found) else "")
    )
    await msg.edit_text(text, reply_markup=inline.word_found_kb(word, new_pos < len(names), can_trap))


@router.callback_query(F.data == "s:wsave")
async def search_word_save(call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext) -> None:
    ids = (await state.get_data()).get("word_ids") or []
    saved = 0
    for search_id in ids:
        saved += await crud.save_finding(session, user.tg_id, search_id)
    if saved:
        await call.answer(f"📁 Сохранено в «Мои находки»: {saved}")
    else:
        await call.answer("Нечего сохранять — запустите поиск заново", show_alert=True)


@router.callback_query(F.data == "s:wtrap")
async def search_word_trap(call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext) -> None:
    if not await _premium_gate(call, user):
        return
    word = (await state.get_data()).get("word")
    if not word or not is_valid_username(word):
        await call.answer("Ловушку на это слово поставить нельзя", show_alert=True)
        return
    if await _trap_limit_reached(call, session, user):
        return
    trap = await crud.add_trap(session, user.tg_id, word)
    if trap:
        await call.answer(f"✅ Ловушка на @{word} установлена — сообщу, как только ник освободится", show_alert=True)
    else:
        await call.answer(f"ℹ️ Ловушка на @{word} уже активна", show_alert=True)


# ───────────────────────── Ловушка на ник ─────────────────────────


def _trap_limit(user: User) -> int | None:
    return None if user.tg_id in settings.admin_ids else MAX_TRAPS


async def _trap_limit_reached(call: CallbackQuery, session: AsyncSession, user: User) -> bool:
    limit = _trap_limit(user)
    if limit is not None and len(await crud.get_user_traps(session, user.tg_id)) >= limit:
        await call.answer(f"Достигнут лимит в {limit} ловушек", show_alert=True)
        return True
    return False


async def _traps_screen(session: AsyncSession, user: User):
    traps = await crud.get_user_traps(session, user.tg_id)
    text = "🪤 <b>ЛОВУШКА НА НИК (СНАЙПЕР)</b>\n\n"
    if traps:
        text += "Активные ловушки:\n"
        for t in traps[:TRAPS_SHOWN]:
            checked = f"{msk(t.last_checked_at):%d.%m %H:%M} МСК" if t.last_checked_at else "ожидает"
            nets = "".join(social_checker.icon(c) for c in trap_networks(t))
            text += f"• <b>@{t.target_username}</b> {nets} — последняя проверка: {checked}\n"
        if len(traps) > TRAPS_SHOWN:
            text += f"…и ещё {len(traps) - TRAPS_SHOWN} — они тоже работают.\n"
        text += "\nНажмите на ник, чтобы удалить ловушку."
    else:
        text += "У вас пока нет активных ловушек.\n\nДобавьте занятый ник — бот сообщит, как только он освободится."
    limit = _trap_limit(user)
    text += f"\n\nЛимит: {len(traps)}/{limit}" if limit is not None else f"\n\nЛовушек: {len(traps)} (без лимита)"
    return text, inline.traps_kb(traps[:TRAPS_SHOWN]), "trap"


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
    if await _trap_limit_reached(call, session, user):
        return
    await call.answer()
    await state.set_state(SearchStates.trap)
    await safe_edit(call, TRAP_PROMPT, inline.cancel_kb("s:trap"))


@router.message(SearchStates.trap, F.text)
async def trap_input(message: Message, session: AsyncSession, user: User, state: FSMContext, checker: UsernameChecker) -> None:
    name = normalize(message.text)
    if not any(net.name_re.fullmatch(name) for net in social_checker.ALL.values() if net.code != "ig"):
        await message.answer(
            "⚠️ Некорректный юзернейм. Допустимы латиница, цифры и «_».",
            reply_markup=inline.cancel_kb("s:trap"),
        )
        return
    wait = await message.answer(f"⏳ Проверяю @{name} в Telegram и соцсетях…")
    statuses = await network_statuses(checker, name)
    # Ловить имеет смысл там, где ник сейчас занят (или сеть не ответила)
    catchable = [code for code, (st, _) in statuses.items() if st in ("taken", "unknown")]
    if not catchable:
        await state.set_state(None)
        await wait.edit_text(
            f"🪤 <b>@{name}</b>\n\n{format_statuses(name, statuses)}\n\n"
            "Ловить негде: там, где ник свободен, — занимайте его сейчас, а где он недоступен — "
            "ловушка не поможет.",
            reply_markup=inline.back_kb("s:trap", "🔙 К ловушкам"),
            disable_web_page_preview=True,
        )
        return
    selected = [catchable[0]] if "tg" not in catchable else ["tg"]
    await state.set_state(None)
    await state.update_data(trap_name=name, trap_catchable=catchable, trap_selected=selected,
                            trap_text=format_statuses(name, statuses))
    await wait.edit_text(_trap_select_text(name, format_statuses(name, statuses)),
                         reply_markup=inline.trap_networks_kb(catchable, selected), disable_web_page_preview=True)


def _trap_select_text(name: str, statuses_text: str) -> str:
    return (
        f"🪤 <b>ЛОВУШКА НА @{name}</b>\n\n{statuses_text}\n\n"
        "Отметьте, где ловить ник — бот сообщит, как только он освободится в выбранных сетях:"
    )


@router.callback_query(F.data.startswith("trap:net:"))
async def trap_toggle_network(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    code = call.data.split(":", 2)[2]
    if not data.get("trap_name") or code not in data.get("trap_catchable", []):
        await call.answer("Начните заново: «➕ Добавить ловушку»", show_alert=True)
        return
    selected = list(data.get("trap_selected", []))
    selected = [c for c in selected if c != code] if code in selected else selected + [code]
    await state.update_data(trap_selected=selected)
    await call.answer()
    await safe_edit(call, _trap_select_text(data["trap_name"], data["trap_text"]),
                    inline.trap_networks_kb(data["trap_catchable"], selected))


@router.callback_query(F.data == "trap:ok")
async def trap_confirm(call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext) -> None:
    if not await _premium_gate(call, user):
        return
    data = await state.get_data()
    name, selected = data.get("trap_name"), data.get("trap_selected") or []
    if not name:
        await call.answer("Начните заново: «➕ Добавить ловушку»", show_alert=True)
        return
    if not selected:
        await call.answer("Отметьте хотя бы одну сеть", show_alert=True)
        return
    # Дополнение сетей в уже существующей ловушке лимит не тратит
    existing = any(t.target_username.lower() == name.lower() for t in await crud.get_user_traps(session, user.tg_id))
    if not existing and await _trap_limit_reached(call, session, user):
        return
    order = [c for c in social_checker.ALL if c in selected]
    trap = await crud.add_trap(session, user.tg_id, name, ",".join(order))
    await state.update_data(trap_name=None)
    if trap:
        where = ", ".join(f"{social_checker.icon(c)} {social_checker.ALL[c].title}" for c in trap_networks(trap))
        text = (
            f"✅ Ловушка на <b>@{name}</b> установлена!\n\n"
            f"Ловим в: {where}\n\n"
            "Как только ник освободится — вы получите мгновенное уведомление 🚨"
        )
    else:
        text = f"ℹ️ Ловушка на <b>@{name}</b> уже ловит ник во всех выбранных сетях."
    await call.answer()
    await safe_edit(call, text, inline.back_kb("s:trap", "🪤 Мои ловушки"))


async def network_statuses(checker: UsernameChecker, name: str) -> dict[str, tuple[str, str]]:
    """Статус ника в Telegram и соцсетях: {код: (статус, подпись)} — статусы как в social_checker."""
    async def telegram() -> tuple[str, str]:
        if not is_valid_username(name):
            return "invalid", social_checker.STATUS_MARK["invalid"]
        with activity.user_search():  # фоновый поиск запаса ников на это время на паузе
            res = await checker.check(name, fresh=True)
        # Зарезервирован Telegram или продан/продаётся на Fragment — сам по себе не освободится,
        # ловить его бессмысленно; ловим только ники, занятые аккаунтом, каналом или ботом
        status = {"free": "free", "unknown": "unknown", "likely": "unknown", "invalid": "invalid", "taken": "taken"}.get(res.status, "reserved")
        label = STATUS_TEXT.get(res.status, res.status)
        if res.fragment_price:
            label += f" ({res.fragment_price} TON)"
        return status, label

    tg, social = await asyncio.gather(telegram(), social_checker.check_all(name))
    result = {"tg": tg}
    for code, st in social.items():
        result[code] = (st, social_checker.STATUS_MARK[st])
    return result


def format_statuses(name: str, statuses: dict[str, tuple[str, str]]) -> str:
    lines = [f"{social_checker.icon(code)} <b>{social_checker.ALL[code].title}</b> — {label}"
             for code, (_, label) in statuses.items()]
    ig = social_checker.INSTAGRAM
    lines.append(f'{social_checker.icon("ig")} <b>Instagram</b> — <a href="{ig.url.format(name=name)}">проверить вручную</a>')
    return "\n".join(lines)


@router.callback_query(F.data.startswith("trap:del:"))
async def trap_delete(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    await crud.delete_trap(session, user.tg_id, int(call.data.split(":")[2]))
    await call.answer("🗑 Ловушка удалена")
    await safe_edit(call, *await _traps_screen(session, user))
