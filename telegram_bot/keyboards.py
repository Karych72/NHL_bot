"""Общие помощники inline-клавиатур бота: сетка кнопок и сообщение с нажатой кнопкой.

Используются хендлерами `bot.py` и `stats_handlers.py` — все экраны с кнопками
собирают сетку через `build_menu` и правят своё сообщение через `callback_message`.
"""

from typing import List

from aiogram.types import CallbackQuery, Message


def callback_message(callback: CallbackQuery) -> Message:
    """Сообщение с нажатой кнопкой — то, которое хендлер правит на месте.

    Зачем: у `CallbackQuery.message` тип `Message | InaccessibleMessage | None`; кнопки
    бота висят только на его сообщениях, а недоступное (удалённое) сообщение править
    нельзя — распаковываем один раз и громко падаем, если инвариант нарушен.
    `callback` — нажатие, пришедшее в хендлер.
    """
    message = callback.message
    assert isinstance(message, Message)
    return message


def build_menu(buttons: list, n_cols: int) -> List[list]:
    """Раскладывает плоский список кнопок `buttons` в сетку для `InlineKeyboardMarkup`
    по `n_cols` в ряд."""
    return [buttons[i:i + n_cols] for i in range(0, len(buttons), n_cols)]
