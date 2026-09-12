"""Рендеринг текстовых шаблонов сообщений бота (`telegram_bot/messages/*.txt`)
через Jinja2.

Часть `telegram_bot/`: `bot_messages.py` собирает словарь значений из БД,
а этот модуль подставляет их в шаблон и возвращает готовый текст сообщения.
"""

from jinja2 import Template


def read_template(file: str) -> str:
    """Читает файл шаблона целиком и возвращает его текст как есть, без
    рендеринга плейсхолдеров.

    Args:
        file: путь к файлу шаблона (обычно `messages/<name>.txt`).
    """
    with open(file, 'r') as f:
        return f.read()


def output_text(template_file: str, render: dict) -> str:
    """Рендерит файл шаблона через Jinja2: читает его `read_template`, затем
    подставляет `render` в плейсхолдеры шаблона.

    Args:
        template_file: путь к файлу шаблона.
        render: значения для подстановки в плейсхолдеры.
    """
    body = read_template(template_file)
    return Template(body).render(render)
