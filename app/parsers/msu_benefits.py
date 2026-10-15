"""Особые права МГУ из PDF «Особые права, предоставляемые победителям и призерам олимпиад
школьников…» (cpk.msu.ru/files/<год>/olymp_benefits.pdf).

Таблица по факультетам, колонки:

    0  направление подготовки           1  профиль олимпиады
    2  предметы/направления             3  перечень олимпиад («*» — все олимпиады уровня)
    4  уровень («I, II»)                5  класс («11», «10, 11»)
    6  победитель/призёр                7  предмет ЕГЭ не менее 75 баллов
    8  льгота: БВИ / 100 баллов ЕГЭ по … / 100 баллов ДВИ по …

Объединённые ячейки pdfplumber отдаёт как None — значение берётся из строки выше.
Нужен pdfplumber (pip install pdfplumber); на машине без него таблицу можно разобрать там, где он есть.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

WILDCARD = "*"
_LEVELS = {"I": 1, "II": 2, "III": 3}
_TYPOS = {"Олимпида": "Олимпиада", "Ломонсоова": "Ломоносова", "Иформатика": "Информатика", "иформатика": "информатика"}


@dataclass
class MsuImport:
    rules: list[dict]
    rows: int
    faculties: int
    title: str = ""
    warnings: list[str] = field(default_factory=list)


def _clean(text: str | None) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"(\w)- (\w)", r"\1-\2", text)  # перенос «бизнес- процессов»
    text = re.sub(r"\b([А-ЯЁ]\.[А-ЯЁ]\.)(?=[А-ЯЁ][а-яё])", r"\1 ", text)  # «М.В.Ломоносова» → «М.В. Ломоносова»
    for wrong, right in _TYPOS.items():
        text = text.replace(wrong, right)
    return text


def split_olympiads(text: str) -> list[str]:
    """«А, Олимпиада «Б, В»; Г» → по «;» и запятым вне кавычек перед словом с заглавной буквы."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "«":
            depth += 1
        elif ch == "»":
            depth = max(0, depth - 1)
        elif depth == 0 and (ch == ";" or ch == "," and re.match(r",\s*[А-ЯЁA-Z«]", text[i:])):
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def parse_levels(text: str) -> list[int]:
    return sorted({_LEVELS[x] for x in re.findall(r"I{1,3}", text) if x in _LEVELS})


def parse_diploma(text: str) -> str | None:
    t = text.lower().replace("ё", "е")
    winner, prize = "победител" in t, "призер" in t
    return "any" if winner and prize else "winner" if winner else "prize" if prize else None


def parse_benefits(text: str) -> list[tuple[str, str | None]]:
    """«…по дополнительному вступительному испытанию по математике; …по ЕГЭ по математике» →
    [("dvi100", "ДВИ по математике"), ("score100", "ЕГЭ по математике")]."""
    result = []
    for part in re.split(r";\s*", text):
        low = part.lower()
        if "без вступительных испытаний" in low:
            result.append(("bvi", None))
        elif "дополнительному вступительному испытанию" in low:
            subject = re.search(r"испытанию\s+по\s+(.+)$", part)
            result.append(("dvi100", f"ДВИ по {subject.group(1)}" if subject else "ДВИ"))
        elif "егэ" in low:
            subject = re.search(r"ЕГЭ\s+по\s+(.+)$", part)
            result.append(("score100", f"ЕГЭ по {subject.group(1)}" if subject else "ЕГЭ"))
    return result


def _faculty(text: str) -> str:
    return text[:1] + text[1:].lower().replace("мгу", "МГУ").replace("г. грозном", "г. Грозном") \
        .replace("г. севастополе", "г. Севастополе")


def parse_msu_rows(pages: list[list[list[str | None]]]) -> MsuImport:
    """pages — таблицы постранично (как отдаёт pdfplumber: строки из 9 ячеек, None — объединённая)."""
    faculty = ""
    prev: list[str] = [""] * 9
    parsed, warnings, faculties = [], [], 0
    for page_no, rows in enumerate(pages, start=1):
        for raw in rows:
            if len(raw) < 9:
                continue
            cells = [None if c is None else _clean(c) for c in raw[:9]]
            filled = [c for c in cells if c]
            if len(filled) == 1 and cells[0] and cells[0].upper() == cells[0] and not any(cells[1:]):
                faculty, prev, faculties = _faculty(cells[0]), [""] * 9, faculties + 1
                continue
            if cells[4] and "Уровень" in cells[4]:
                continue  # шапка
            row = [prev[i] if c is None or (c == "" and i in (0, 1, 2, 3)) else c for i, c in enumerate(cells)]
            prev = row
            program, profile, _, olympiads, level, grade, diploma, confirm, benefit = row
            if not benefit or not olympiads:
                continue
            kinds = parse_benefits(benefit)
            dip = parse_diploma(diploma)
            if not kinds or dip is None:
                warnings.append(f"стр. {page_no}: не разобрано «{diploma}» / «{benefit}»")
                continue
            names = [WILDCARD] if olympiads.strip() == WILDCARD else split_olympiads(olympiads)
            for name in names:
                for kind, score_for in kinds:
                    parsed.append({
                        "olympiad": name,
                        "profile": profile.lower(),
                        "levels": parse_levels(level) or None,
                        "grades": [int(x) for x in re.findall(r"\d+", grade)] or None,
                        "diploma": dip,
                        "confirm": confirm.lower() or None,
                        "benefit": kind,
                        "score_for": score_for,
                        "program": f"{faculty}: {program}" if faculty else program,
                    })

    groups: dict[tuple, dict] = {}
    for p in parsed:
        key = (p["olympiad"], p["profile"], tuple(p["levels"] or ()), tuple(p["grades"] or ()),
               p["diploma"], p["confirm"], p["benefit"], p["score_for"])
        g = groups.setdefault(key, {**p, "programs": []})
        if p["program"] not in g["programs"]:
            g["programs"].append(p["program"])
    rules = []
    for g in groups.values():
        rule = {
            "olympiad": g["olympiad"],
            "profiles": [g["profile"]] if g["profile"] else None,
            "levels": g["levels"],
            "benefit": g["benefit"],
            "diploma": g["diploma"],
            "grades": g["grades"],
            "confirm_min_score": 75,
            "programs": g["programs"],
        }
        if g["confirm"] and g["confirm"] != g["profile"]:
            rule["confirm_subject"] = g["confirm"]
        if g["score_for"]:
            rule["score_for"] = g["score_for"]
        rules.append(rule)
    return MsuImport(rules=rules, rows=len(parsed), faculties=faculties, warnings=warnings)


def parse_msu_pdf(path) -> MsuImport:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Нужен pdfplumber: py -m pip install pdfplumber") from exc
    with pdfplumber.open(path) as pdf:
        pages = [[row for table in page.extract_tables() for row in table] for page in pdf.pages]
        first = pdf.pages[0].extract_text() or ""
    result = parse_msu_rows(pages)
    result.title = re.sub(r"\s+", " ", first.split("Установлены")[0]).strip()
    return result

