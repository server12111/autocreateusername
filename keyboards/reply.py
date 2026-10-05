from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

CANCEL_TEXT = "❌ Отмена"

# Reply-клавиатура для пошаговых диалогов админки
cancel_reply_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=CANCEL_TEXT)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

remove_kb = ReplyKeyboardRemove()
