"""Парсер перечня олимпиад с сайта Российского совета олимпиад школьников.

Страницы:
    https://rsr-olymp.ru/              — текущий перечень
    https://rsr-olymp.ru/archive/2025  — перечень 2025/26 (год в адресе = первый год учебного года)

Разметка (проверено 15.10.2026): таблица ``table.mainTableInfo`` с колонками
«№ | Название | Профиль | Предметы | Уровень». У олимпиады с несколькими
профилями ячейки «№» и «Название» растянуты через rowspan. Одна и та же
олимпиада может встретиться несколькими группами строк с одинаковым номером,
если у разных профилей разные сайты (например, «Росатом»).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from app.parsers.subjects import normalize_text, split_subjects

RSR_BASE_URL = "https://rsr-olymp.ru"
TABLE_SELECTOR = "table.mainTableInfo"

_YEAR_IN_TEXT_RE = re.compile(r"на\s+(\d{4})\s*/\s*(\d{2})\s+учебный\s+год")
_ARCHIVE_URL_RE = re.compile(r"/archive/(\d{4})/?$")


class RsrParseError(Exception):
    """Страница не похожа на перечень РСОШ (изменилась вёрстка?)."""


@dataclass
class Profile:
    profile: str
    subjects_raw: str
    subjects: list[str]
    level: int | None
    website: str | None


@dataclass
class Olympiad:
    number: int | None
    name: str
    key: str
    profiles: list[Profile] = field(default_factory=list)


@dataclass
class RsrList:
    academic_year: str
    source_url: str | None
    parsed_at: str
    olympiads: list[Olympiad]
    warnings: list[str]

    @property
    def profile_count(self) -> int:
        return sum(len(o.profiles) for o in self.olympiads)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RsrList":
        olympiads = [
            Olympiad(
                number=o["number"],
                name=o["name"],
                key=o["key"],
                profiles=[Profile(**p) for p in o["profiles"]],
            )
            for o in data["olympiads"]
        ]
        return cls(
            academic_year=data["academic_year"],
            source_url=data.get("source_url"),
            parsed_at=data.get("parsed_at", ""),
            olympiads=olympiads,
            warnings=list(data.get("warnings", [])),
        )


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def olympiad_key(name: str) -> str:
    """Ключ олимпиады, стабильный между годами.

    Убирает порядковый номер римскими цифрами («XIX Южно-Российская…» и
    «XVIII Южно-Российская…» — одна олимпиада), кавычки, регистр и лишние пробелы.
    """
    text = normalize_text(name)
    text = re.sub(r"^[IVXLCDM]+\s+", "", text)
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[«»\"“”„]", "", text)
    text = re.sub(r"[–—]", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def academic_year_from_start(start_year: int) -> str:
    return f"{start_year}/{(start_year + 1) % 100:02d}"


def detect_academic_year(html: str, url: str | None = None) -> str | None:
    """Определяет учебный год перечня.

    1. Если адрес вида /archive/2025 — год берётся из адреса.
    2. Иначе ищется последняя фраза «на 2026/27 учебный год» перед таблицей.
       Последняя — потому что выше таблицы есть блок «Актуально» со ссылкой
       на свежую новость, и на архивных страницах там стоит другой год.
    """
    if url:
        match = _ARCHIVE_URL_RE.search(url)
        if match:
            return academic_year_from_start(int(match.group(1)))
    table_pos = html.find("mainTableInfo")
    head = html if table_pos < 0 else html[:table_pos]
    head_text = BeautifulSoup(head, "lxml").get_text(" ")
    matches = _YEAR_IN_TEXT_RE.findall(head_text)
    if not matches:
        return None
    start, end = matches[-1]
    year = f"{start}/{end}"
    if academic_year_from_start(int(start)) != year:
        return None
    return year


def _expand_rows(table: Tag) -> list[list[Tag]]:
    """Разворачивает таблицу с rowspan/colspan в прямоугольную сетку ячеек."""
    grid: list[list[Tag]] = []
    carry: dict[int, list] = {}  # колонка -> [ячейка, сколько строк ещё занимает]
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        row: list[Tag] = []
        col = 0
        i = 0
        while i < len(cells) or col in carry:
            if col in carry:
                cell, left = carry[col]
                row.append(cell)
                if left <= 1:
                    del carry[col]
                else:
                    carry[col][1] = left - 1
                col += 1
                continue
            cell = cells[i]
            i += 1
            rowspan = _int_attr(cell, "rowspan")
            colspan = _int_attr(cell, "colspan")
            for _ in range(colspan):
                row.append(cell)
                if rowspan > 1:
                    carry[col] = [cell, rowspan - 1]
                col += 1
        grid.append(row)
    return grid


def _int_attr(cell: Tag, name: str) -> int:
    try:
        return max(1, int(cell.get(name, 1)))
    except (TypeError, ValueError):
        return 1


def _cell_text(cell: Tag) -> str:
    return normalize_text(cell.get_text(" "))


def _is_header(row: list[Tag]) -> bool:
    first = _cell_text(row[0]) if row else ""
    return first.startswith("№") or any(_cell_text(c) == "Название" for c in row)


# ---------------------------------------------------------------------------
# Основной разбор
# ---------------------------------------------------------------------------

def parse_rsr_html(html: str, *, url: str | None = None, academic_year: str | None = None) -> RsrList:
    """Разбирает HTML страницы перечня в структуру RsrList."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one(TABLE_SELECTOR)
    if table is None:
        raise RsrParseError(f"Не найдена таблица {TABLE_SELECTOR}: вёрстка сайта РСОШ изменилась?")

    year = academic_year or detect_academic_year(html, url)
    if not year:
        raise RsrParseError("Не удалось определить учебный год перечня, укажите его явно")

    warnings: list[str] = []
    olympiads: dict[str, Olympiad] = {}

    for row_index, row in enumerate(_expand_rows(table)):
        if not row or _is_header(row):
            continue
        if len(row) != 5:
            warnings.append(f"Строка {row_index}: ожидалось 5 колонок, найдено {len(row)}, пропущена")
            continue

        number_cell, name_cell, profile_cell, subjects_cell, level_cell = row
        name = _cell_text(name_cell)
        if not name:
            warnings.append(f"Строка {row_index}: пустое название олимпиады, пропущена")
            continue

        number_text = _cell_text(number_cell)
        number = int(number_text) if number_text.isdigit() else None
        link = name_cell.find("a", href=True)
        website = link["href"].strip() if link else None

        key = olympiad_key(name)
        olympiad = olympiads.get(key)
        if olympiad is None:
            olympiad = Olympiad(number=number, name=name, key=key)
            olympiads[key] = olympiad
        elif olympiad.number != number:
            warnings.append(
                f"Олимпиада «{name}» встречается под номерами {olympiad.number} и {number}"
            )

        profile_name = _cell_text(profile_cell).lower()
        subjects_raw = _cell_text(subjects_cell)
        subjects, subject_warnings = split_subjects(subjects_raw)
        warnings.extend(subject_warnings)

        level_text = _cell_text(level_cell)
        level: int | None = int(level_text) if level_text.isdigit() else None
        if level not in (1, 2, 3):
            warnings.append(f"«{name}», профиль «{profile_name}»: неожиданный уровень «{level_text}»")
            level = None

        if any(p.profile == profile_name for p in olympiad.profiles):
            warnings.append(f"«{name}»: профиль «{profile_name}» повторяется, оставлен первый")
            continue

        olympiad.profiles.append(
            Profile(
                profile=profile_name,
                subjects_raw=subjects_raw,
                subjects=subjects,
                level=level,
                website=website,
            )
        )

    if not olympiads:
        raise RsrParseError("Таблица перечня пуста")

    return RsrList(
        academic_year=year,
        source_url=url,
        parsed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        olympiads=list(olympiads.values()),
        warnings=warnings,
    )


def rsr_url_for_year(start_year: int | None) -> str:
    """Адрес страницы перечня: None — текущий, иначе архив за год."""
    if start_year is None:
        return RSR_BASE_URL + "/"
    return f"{RSR_BASE_URL}/archive/{start_year}"
