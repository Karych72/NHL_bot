"""Примитивы разметки и общие константы сообщений бота.

Часть `telegram_bot/` без доступа к БД: таблица `_table`, построчный HTML → rich-сообщение
(`rich_message`, Bot API 10.1+), подвал/свёрнутый блок/список, форматтеры чисел, страница
лидерборда, обрезка длинного текста под лимит Telegram, `MSK` и справочник стран
`COUNTRY_LABELS`. Экранные модули (`game_messages`, `team_messages`, `leaderboard_messages`,
`country_messages`, `bot_messages`) и обработчики строят сообщения из этих кирпичей.
"""

import html
from datetime import timedelta, timezone
from decimal import Decimal
from typing import Collection, Dict, List, Optional, Sequence, Tuple, Union

from aiogram.types import InputRichMessage, InputRichMessageMedia

import config


# Лимит текста rich-сообщения Telegram (символов). Считается по исходному HTML: он не
# короче видимого текста, так что сообщение в лимите заведомо проходит. Второй лимит —
# 500 блоков (абзац, строка таблицы) — обрезкой не охраняется: самый длинный экран
# (/standings, ~70 блоков) далёк от него, а длинные списки листаются страницами.
RICH_MESSAGE_MAX_LENGTH = 32768


def _format_leader_value(value: Union[int, float, Decimal, str, None]) -> str:
    """Plain-text display for leaderboard cells (no monospace padding)."""
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        fv = float(value)
        if abs(fv - round(fv)) < 1e-9:
            return str(int(round(fv)))
        s = f"{fv:.3f}".rstrip("0").rstrip(".")
        return s
    return str(value)


def _table(
    rows: Sequence[Sequence[str]],
    align: str,
    *,
    playoff_line_after: Optional[int] = None,
    bold: Collection[Tuple[int, int]] = (),
) -> str:
    """Таблица rich-сообщения (Bot API 10.3): первая строка `rows` — шапка, остальные — тело.

    Зачем: колонки rich-таблицы Telegram раскладывает сам по ширине экрана — без
    моноширинного `<pre>`, выравнивания пробелами и выбрасывания колонок под телефон.
    `compact` — меньшие отступы в ячейках; `striped` (чередование строк) — только у
    таблиц длиннее `_STRIPED_MIN_ROWS` строк.
    Таблица — одна строка текста: `rich_message()` делает её отдельным блоком.

    Аргументы:
        rows: ячейки (сырые строки, без HTML); у всех строк одно число колонок.
        align: по символу на колонку — `l` (влево) или `r` (вправо).
        playoff_line_after: после стольких строк тела — пунктир `_PLAYOFF_LINE` на всю
            ширину (турнирная таблица: топ-3 дивизиона, две путёвки Wild Card).
        bold: ячейки тела жирным — пары (строка тела, колонка), с нуля; то, на что
            экран отвечает (лучшее значение превью матча).
    """
    def row(cells: Sequence[str], tag: str, r: int = -1) -> str:
        def cell(c: str, a: str, col: int) -> str:
            text = f"<b>{html.escape(c)}</b>" if (r, col) in bold else html.escape(c)
            return f'<{tag} align="right">{text}</{tag}>' if a == "r" else f"<{tag}>{text}</{tag}>"

        return "<tr>" + "".join(cell(c, a, col) for col, (c, a) in enumerate(zip(cells, align))) + "</tr>"

    head, *body = rows
    parts = [row(head, "th")]
    for i, cells in enumerate(body):
        if i == playoff_line_after:
            parts.append(f'<tr><td colspan="{len(align)}" align="center">{_PLAYOFF_LINE}</td></tr>')
        parts.append(row(cells, "td", i))
    attrs = "compact striped" if len(body) > _STRIPED_MIN_ROWS else "compact"
    return f"<table {attrs}>" + "".join(parts) + "</table>"


# Линия плей-офф: рамок у отдельных строк rich-таблица не знает, поэтому черта — строка
# пунктира на всю ширину; длина — чтобы не раздвигать таблицу на телефоне.
_PLAYOFF_LINE = "┄" * 28

# Полосы — только у таблиц длиннее десятка строк: на длинной они ведут глаз по строке,
# на короткой — лишний шум (spike Задачи 88, `docs/telegram_bot.md` §«Стиль сообщений»).
_STRIPED_MIN_ROWS = 10

# Видимый зазор между абзацами: граница `<p>` в клиентах Telegram отступа не даёт,
# а пустой `<p></p>` схлопывается (spike Задачи 88).
_PARAGRAPH_GAP = "<p>&nbsp;</p>"


# Строки-блоки, которые `rich_message()` не вкладывает в абзац: таблица, свёрнутый блок,
# список, подвал, лента видео голов — каждый собран своим хелпером в одну строку.
_BLOCK_TAGS = ("<table", "<details", "<ul", "<footer", "<tg-slideshow")

# После каких блоков пустая строка даёт видимый зазор перед абзацем: абзац и свёрнутый блок
# («Видео голов» перед «Броски», свёрнутая карточка перед блоком страны). После таблиц зазора нет.
_GAP_AFTER = ("<p>", "<details")


def _rich_html(text: str) -> str:
    """Построчный HTML → rich-HTML блоков; правила — у `rich_message()`."""
    blocks: List[str] = []
    paragraph: List[str] = []
    gap = False

    def flush() -> None:
        nonlocal gap
        if not paragraph:
            return
        if gap and blocks and blocks[-1].startswith(_GAP_AFTER):
            blocks.append(_PARAGRAPH_GAP)
        blocks.append("<p>" + "<br>".join(paragraph) + "</p>")
        paragraph.clear()
        gap = False

    for line in text.split("\n"):
        if line.startswith(_BLOCK_TAGS):
            flush()
            blocks.append(line)
        elif line.strip():
            paragraph.append(line)
        else:
            flush()
            gap = True
    flush()
    return "".join(blocks)


def rich_message(
    text: str, media: Optional[List[InputRichMessageMedia]] = None
) -> InputRichMessage:
    """Текст бота → rich-сообщение (Bot API 10.1): блок строк — абзац `<p>` с переносами
    `<br>`, пустая строка — граница абзацев с видимым зазором, строка-блок (`_table`,
    `rich_details`, `rich_list`, `rich_footer`) — свой блок.

    Зачем: в rich-HTML перевод строки — пробел, поэтому строки соединяются `<br>`;
    пустые строки, которыми шаблоны и сборщики делят сообщение на разделы, остаются
    зазором. Тег не должен пересекать пустую строку: блоки — законченные фрагменты
    разметки, иначе Telegram отвергнет сообщение.

    Аргументы:
        text: построчный HTML, как его собирают функции этого модуля.
        media: клипы голов карточки матча — по `id` из `goal_clip_media_id`, на которые
            ссылаются её строки голов (`game_message`).
    """
    return InputRichMessage(html=_rich_html(text), media=media or None)


def rich_footer(text: str) -> str:
    """Подвал сообщения — строка-блок `<footer>` для конца текста `rich_message()`.

    Зачем: служебный хвост (ссылки на настройки и соседние экраны, сезон, «данные на …»)
    клиенты рисуют мельче и серее содержания (`docs/telegram_bot.md` §«Стиль сообщений»,
    правило 3).

    Аргументы: `text` — построчный HTML подвала; строки соединяются `<br>`. Подвал без
    строк (превью матча, команды которого не сопоставлены) — пустая строка, не `<footer></footer>`.
    """
    lines = [line for line in text.split("\n") if line.strip()]
    return "<footer>" + "<br>".join(lines) + "</footer>" if lines else ""


def rich_details(summary: str, text: str) -> str:
    """Свёрнутый блок `<details>` — строка-блок для текста `rich_message()`.

    Зачем: легенды обозначений и расшифровки метрик нужны не при каждом открытии экрана —
    по умолчанию свёрнуты, кто не знает — раскроет (правило 4 стиля).

    Аргументы:
        summary: подпись свёрнутого блока (уже экранированная).
        text: построчный HTML содержимого — по тем же правилам, что у `rich_message()`.
    """
    return f"<details><summary>{summary}</summary>{_rich_html(text)}</details>"


def _season_footer() -> str:
    """Подвал справочного экрана, у которого из служебного — только сезон."""
    return rich_footer(f"Сезон {html.escape(str(config.CURRENT_SEASON))}")


def rich_list(items: Sequence[str]) -> str:
    """Маркированный список `<ul>` — строка-блок для текста `rich_message()`.

    Зачем: перечень команд `/help` — список с отступом у переносов длинных пунктов,
    а не строки абзаца.

    Аргументы: `items` — пункты, уже готовый инлайн-HTML.
    """
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def _record_str(
    wins: Union[int, float, Decimal, None],
    losses: Union[int, float, Decimal, None],
    ot: Union[int, float, Decimal, None],
) -> str:
    """Баланс «W-L-OT» (`7-3-0`) — общий для профиля клуба, превью матча и вратарей."""
    return "-".join(_format_leader_value(v) for v in (wins, losses, ot))


def playoff_label(game_id: int) -> Optional[str]:
    """Пометка матча плей-офф «Плей-офф, раунд R, игра G» или `None` для других типов.

    Зачем: карточка, сводка дайджеста и превью `/tonight` показывают, что матч —
    плей-офф (Задача 106). Всё берётся из `game_id` (`2025030123` — тип 03, раунд 1,
    серия 2, игра 3), без запроса к API и без колонки в базе.

    Аргументы:
        game_id: десятизначный id матча NHL.
    """
    digits = str(game_id)
    if digits[4:6] != "03":
        return None
    return f"Плей-офф, раунд {digits[7]}, игра {digits[9]}"


def _fmt_num_max2(val: Union[int, float, None]) -> str:
    if val is None:
        return "—"
    r = round(float(val), 2)
    s = f"{r:.2f}"
    return s.rstrip("0").rstrip(".")


def _fmt_pct(val: Union[int, float, Decimal, None]) -> str:
    """Проценты (доля очков, PP/PK, вбрасывания, % отражённых) — в БД уже в шкале 0–100
    (нормализует пайплайн, `optional_pct_from_ratio`)."""
    if val is None:
        return "—"
    r = round(float(val), 2)
    s = f"{r:.2f}"
    return s.rstrip("0").rstrip(".") + "%"


def plural_word(value: Union[int, float, Decimal, None], forms: Tuple[str, str, str]) -> str:
    """Слово, согласованное с числом (1 очко, 2 очка, 5 очков): `forms` — формы
    для 1, 2 и 5; `None` считается нулём."""
    n = int(value or 0)
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _fmt_signed(val: Union[int, float, None]) -> str:
    """Число со знаком («+1», «-0.5», «0») — разница шайб: плюс виден так же
    явно, как минус."""
    s = _fmt_num_max2(val)
    return f"+{s}" if val is not None and float(val) > 0 else s


LEADERBOARD_PAGE_SIZE = 10


def _rank_range_label(offset: int, rows: int) -> str:
    if rows <= 0:
        return "нет данных"
    lo = offset + 1
    hi = offset + rows
    if lo == hi:
        return str(lo)
    return f"{lo}–{hi}"


def _leaderboard_page_text(
    heading_html: str, body: str, n: int, total: int, offset: int
) -> Tuple[str, bool, bool]:
    """Общий каркас страницы лидерборда: заголовок, тело, подвал с сезоном и маркером
    «Показаны N из M» и флаги перелистывания.

    Аргументы:
        heading_html: уже экранированный заголовок (без тегов).
        body: строки страницы; при `n == 0` заменяются текстом «нет данных».
        n: число строк на странице; total: размер полной выборки; offset: сдвиг.

    Возвращает: (текст, has_prev, has_next).
    """
    span = _rank_range_label(offset, n)
    if n == 0:
        body = "<i>Нет данных в этом диапазоне.</i>"
        marker = f"<i>{html.escape(span)}</i>"
    else:
        marker = truncation_marker(html.escape(span), total, item_word="строк")
    text = f"<b>{heading_html}</b>\n{body}\n" + rich_footer(
        f"Сезон {html.escape(str(config.CURRENT_SEASON))} · {marker}"
    )
    # total, а не «страница пришла полной» — иначе последняя полная страница
    # рисует кнопку «вперёд» на пустой диапазон, противореча маркеру.
    return text, offset > 0, offset + n < total


# Задача 42: страна игрока — rosters.nationality (трёхбуквенный код NHL, одна на
# игрока). Справочник покрывает все коды, встречающиеся в БД за все сезоны;
# неизвестный код показывается как есть.
COUNTRY_LABELS: Dict[str, str] = {
    "CAN": "🇨🇦 Канада", "USA": "🇺🇸 США", "SWE": "🇸🇪 Швеция", "RUS": "🇷🇺 Россия",
    "FIN": "🇫🇮 Финляндия", "CZE": "🇨🇿 Чехия", "CHE": "🇨🇭 Швейцария",
    "DEU": "🇩🇪 Германия", "SVK": "🇸🇰 Словакия", "DNK": "🇩🇰 Дания",
    "BLR": "🇧🇾 Беларусь", "LVA": "🇱🇻 Латвия", "AUT": "🇦🇹 Австрия",
    "FRA": "🇫🇷 Франция", "GBR": "🇬🇧 Великобритания", "NOR": "🇳🇴 Норвегия",
    "AUS": "🇦🇺 Австралия", "NLD": "🇳🇱 Нидерланды", "BGR": "🇧🇬 Болгария",
    "SVN": "🇸🇮 Словения", "UZB": "🇺🇿 Узбекистан", "KAZ": "🇰🇿 Казахстан",
    "POL": "🇵🇱 Польша", "BEL": "🇧🇪 Бельгия", "EST": "🇪🇪 Эстония",
}


def country_label(code: str) -> str:
    """Метка страны для HTML: флаг + русское название из `COUNTRY_LABELS`,
    неизвестный код — сам код; результат экранирован. Флаг — метка в начале
    сообщения (заголовок экрана страны)."""
    return html.escape(COUNTRY_LABELS.get(code, code))


def country_name(code: str) -> str:
    """Название страны без флага, сырой строкой (в HTML — через `html.escape`): метка
    `COUNTRY_LABELS` — всегда «флаг название», неизвестный код — сам код.

    Зачем: эмодзи только как метка в начале сообщения (`docs/telegram_bot.md`
    §«Стиль сообщений», правило 5) — в ячейках таблиц, заголовках блоков страны и
    подвале рассылки (`push_digest_job`) флага нет.
    """
    return COUNTRY_LABELS[code].split(" ", 1)[1] if code in COUNTRY_LABELS else code


# Москва живёт в UTC+3 без перехода на летнее время (с 2014 года) — фиксированный
# сдвиг вместо zoneinfo, которому в slim-образе может не хватить tzdata.
MSK = timezone(timedelta(hours=3))


def truncation_marker(
    shown: Optional[Union[int, str]] = None,
    total: Optional[int] = None,
    *,
    item_word: str = "",
) -> str:
    """Единственная точка, где собирается курсивная HTML-сноска об усечении.

    Зачем: обрезка по лимиту Telegram (`truncate_telegram_text`) и обрезка
    выборки (страницы лидербордов, список аббревиатур команд) не должны каждая
    заново собирать текст сноски — иначе формат расползается по веткам
    (Задача 11).

    Экранирование: `shown` вставляется в HTML как есть — экранировать его
    (если это строка из непроверенного источника) обязана вызывающая сторона
    до передачи сюда, как делают лидерборды (`html.escape(span)`).

    Аргументы:
        shown: сколько показано — число или диапазон («1–10»); None — сноска
            без чисел, для обрезки прозаического текста, где считать элементы
            нечем (нет счётных элементов, N/M выдумывать не из чего).
        total: всего элементов в исходной выборке; обязателен, если задан
            `shown` — иначе неоднозначно, что означает «показаны N из ничего».
        item_word: существительное в родительном падеже множественного числа
            («строк», «матчей», «команд»); пусто, если числа сами по себе ясны.

    Исключения:
        ValueError: если `shown` задан, а `total` — нет.
    """
    if shown is None:
        return "<i>Текст обрезан (лимит Telegram).</i>"
    if total is None:
        raise ValueError("truncation_marker: total обязателен, если задан shown")
    word = f" {item_word}" if item_word else ""
    return f"<i>Показаны {shown} из {total}{word}.</i>"


def _telegram_cut_budget(
    note: str, max_len: int = RICH_MESSAGE_MAX_LENGTH
) -> int:
    """Сколько символов исходного текста остаётся под обрезку `truncate_telegram_text`
    при данной сноске `note`.

    Зачем: `digest_shown_match_count` предсказывает результат
    `truncate_telegram_text` заранее (чтобы посчитать N для маркера), поэтому
    обе функции обязаны считать бюджет одной и той же формулой — раздельные
    копии рассинхронизируются при правке одной из них и заставят маркер
    молча врать про число показанных элементов.

    Аргументы:
        note: сноска, которая будет дописана после обрезки (с учётом
            разделителя "\\n\\n", если он нужен).
        max_len: лимит символов сообщения (по умолчанию
            `RICH_MESSAGE_MAX_LENGTH`).
    """
    return max(max_len - len(note), 80)


def truncate_telegram_text(
    text: str,
    max_len: int = RICH_MESSAGE_MAX_LENGTH,
    *,
    footer_note: Optional[str] = None,
) -> str:
    """Режет `text` под лимит Telegram (по умолчанию — rich-сообщения), добавляя сноску.

    Зачем: сообщения Telegram не долетают за лимитом символов; резать нужно с
    запасом под сноску и добавлять её саму, а не отправлять оборванный текст
    без предупреждения. Режет по границе строки: строка — законченный фрагмент
    разметки rich-сообщения (`rich_message`), и её обрывок оставил бы незакрытый тег.

    Аргументы:
        text: исходный текст сообщения (построчный HTML).
        max_len: лимит символов (по умолчанию `RICH_MESSAGE_MAX_LENGTH`).
        footer_note: сноска, дописываемая после обрезки; по умолчанию — общая
            сноска о лимите Telegram без чисел (`truncation_marker()`).
    """
    if len(text) <= max_len:
        return text
    note = footer_note if footer_note is not None else ("\n\n" + truncation_marker())
    cut = _telegram_cut_budget(note, max_len)
    return text[: max(text.rfind("\n", 0, cut + 1), 0)] + note
