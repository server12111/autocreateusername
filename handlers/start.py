import random
import time

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from database import crud
from database.models import User
from handlers.sections import SECTIONS, build_main, safe_edit
from keyboards.inline import captcha_kb, op_kb
from services.op_manager import check_subscription
from texts import CAPTCHA_TEXT, OP_TEXT

router = Router(name="start")

MAX_FAILS = 3
BLOCK_SECONDS = 300
# tg_id -> {"answer": int, "fails": int, "blocked_until": float}
_captcha: dict[int, dict] = {}


def _new_captcha(user_id: int) -> tuple[str, list[int]]:
    a, b = random.randint(1, 9), random.randint(1, 9)
    if random.random() < 0.5:
        question, answer = f"{a} + {b}", a + b
    else:
        a, b = max(a, b), min(a, b)
        question, answer = f"{a} − {b}", a - b
    options = {answer}
    while len(options) < 6:
        options.add(random.randint(0, 18))
    opts = list(options)
    random.shuffle(opts)
    st = _captcha.setdefault(user_id, {"fails": 0, "blocked_until": 0.0})
    st["answer"] = answer
    return CAPTCHA_TEXT.format(question=question), opts


async def show_entry(event: Message | CallbackQuery, bot: Bot, session: AsyncSession, user: User) -> None:
    """После капчи: проверка ОП, затем главное меню."""
    missing = await check_subscription(bot, session, user)
    if missing:
        await safe_edit(event, OP_TEXT, op_kb(missing))
        return
    await safe_edit(event, *await build_main(session, user, bot))


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    bot: Bot,
    session: AsyncSession,
    user: User,
    is_new_user: bool,
    state: FSMContext,
) -> None:
    await state.clear()
    args = command.args or ""
    if is_new_user and args.startswith("ref_") and args[4:].isdigit():
        ref_id = int(args[4:])
        if ref_id != user.tg_id and await crud.get_user(session, ref_id):
            user.referrer_id = ref_id
            await session.commit()

    if not user.is_captcha_passed:
        st = _captcha.get(user.tg_id)
        if st and st["blocked_until"] > time.time():
            left = int(st["blocked_until"] - time.time())
            await message.answer(f"⛔️ Слишком много неверных ответов. Попробуйте через {left // 60 + 1} мин.")
            return
        text, opts = _new_captcha(user.tg_id)
        await message.answer(text, reply_markup=captcha_kb(opts))
        return

    await show_entry(message, bot, session, user)


@router.callback_query(F.data.startswith("cap:"))
async def captcha_answer(call: CallbackQuery, bot: Bot, session: AsyncSession, user: User) -> None:
    if user.is_captcha_passed:
        await call.answer()
        await show_entry(call, bot, session, user)
        return

    st = _captcha.get(user.tg_id)
    if not st or "answer" not in st:
        await call.answer("Капча устарела, отправьте /start", show_alert=True)
        return
    if st["blocked_until"] > time.time():
        await call.answer("⛔️ Вы временно заблокированы. Попробуйте позже.", show_alert=True)
        return

    if int(call.data.split(":")[1]) == st["answer"]:
        _captcha.pop(user.tg_id, None)
        user.is_captcha_passed = True
        await session.commit()
        await call.answer("✅ Проверка пройдена!")
        await show_entry(call, bot, session, user)
        return

    st["fails"] += 1
    if st["fails"] >= MAX_FAILS:
        st["fails"] = 0
        st["blocked_until"] = time.time() + BLOCK_SECONDS
        st.pop("answer", None)
        await call.answer()
        await safe_edit(call, "⛔️ 3 неверных ответа подряд. Доступ заблокирован на 5 минут.\n\nЗатем отправьте /start")
        return
    await call.answer(f"❌ Неверно! Осталось попыток: {MAX_FAILS - st['fails']}", show_alert=True)
    text, opts = _new_captcha(user.tg_id)
    await safe_edit(call, text, captcha_kb(opts))


@router.callback_query(F.data == "check_op_sub")
async def check_op(call: CallbackQuery, bot: Bot, session: AsyncSession, user: User, state: FSMContext) -> None:
    missing = await check_subscription(bot, session, user, use_cache=False)
    if missing:
        await call.answer("❌ Вы подписались не на все каналы. Пожалуйста, завершите подписку.", show_alert=True)
        await safe_edit(call, OP_TEXT, op_kb(missing))
        return
    await call.answer("✅ Подписка подтверждена! Добро пожаловать.", show_alert=True)
    pending = (await state.get_data()).get("op_pending")
    await state.update_data(op_pending=None)
    builder = SECTIONS.get(pending, build_main)
    await safe_edit(call, *await builder(session, user, bot))


@router.callback_query(F.data == "menu:main")
async def main_menu(call: CallbackQuery, bot: Bot, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.set_state(None)
    await call.answer()
    await show_entry(call, bot, session, user)


# Подключается последним: отвечает на сообщения, которые не обработал ни один раздел
fallback_router = Router(name="fallback")


@fallback_router.message(StateFilter(None))
async def unknown_message(message: Message, bot: Bot, session: AsyncSession, user: User) -> None:
    await show_entry(message, bot, session, user)
