"""Особые права Финансового университета: «Перечень олимпиад школьников за 10, 11 класс…» (PDF).

Колонки: № | олимпиада | профиль | предмет ЕГЭ/ВИ | группа программ | БВИ (+/-) | 100 баллов (+/-) |
уровень олимпиады в 2025/26, 2024/25, 2023/24, 2022/23, 2021/22 («-» — диплом этого года не учитывается).

Условия со страницы вуза: дипломы за 10–11 класс, ЕГЭ по предмету от 75, победителям и призёрам.
Номера из соседней колонки в PDF иногда «влезают» в название («о4л импиада») — вычищаем.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

YEARS = ["2025/26", "2024/25", "2023/24", "2022/23", "2021/22"]


@dataclass
class FaImport:
    rules: list[dict]
    rows: int
    warnings: list[str] = field(default_factory=list)


def clean_name(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"(?<=[А-Яа-яЁё«])\d+([А-Яа-яЁё]+) (?=[а-яё])", r"\1", text)  # «о4л импиада» → «олимпиада»
    text = re.sub(r"(?<=[А-Яа-яЁё«])\d+(?=[А-Яа-яЁё])", "", text)
    text = re.sub(r"(?<=[А-ЯЁа-яё])\d+ (?=[а-яё])", "", text)                  # «Т6 олстовская» → «Толстовская»
    text = re.sub(r"\s\d+\s(?=[«A-Za-zА-Яа-яЁё])", " ", text)                 # «… / 3 «Третье», «Innopolis 8 Open»
    text = re.sub(r"(\w)- (\w)", r"\1-\2", text)
    return text.strip()


def parse_fa_rows(rows: list[list[str | None]]) -> FaImport:
    entries: list[dict] = []
    current: dict | None = None
    prev = [""] * 12
    count = 0
    for raw in rows:
        if len(raw) != 12:
            continue
        cells = [None if c is None else re.sub(r"\s+", " ", c).strip() for c in raw]
        if cells[1] and cells[1].startswith("Полное наименование") or (cells[7] or "").startswith("6 2 0 2"):
            continue
        if cells[0] == "" and cells[1] and not any(cells[2:]):
            if current:
                current["name"] += " " + cells[1]  # продолжение названия на следующей странице
            continue
        if cells[0] and cells[0].isdigit():
            current = {"name": cells[1] or "", "rows": []}
            entries.append(current)
            prev = [""] * 12
        if current is None:
            continue
        row = [prev[i] if c is None else c for i, c in enumerate(cells)]
        prev = row
        profile, subject, group, bvi, score = row[2], row[3], row[4], row[5], row[6]
        if not subject or not group:
            continue
        current["rows"].append((profile, subject, group, bvi, score, row[7:12]))
        count += 1

    rules = []
    for e in entries:
        name = clean_name(e["name"])
        for profile, subject, group, bvi, score, levels in e["rows"]:
            by_year = {y: lv for y, lv in zip(YEARS, levels) if lv and lv.strip().isdigit()}
            if not by_year:
                continue
            years_note = "уровень по годам диплома: " + ", ".join(f"{y} — {lv}" for y, lv in by_year.items())
            programs = None if group.lower().startswith("все") else [group.replace("Группа", "группа программ")]
            for benefit, mark in (("bvi", bvi), ("score100", score)):
                if mark != "+":
                    continue
                rule = {
                    "olympiad": name,
                    "profiles": [profile.lower()] if profile else None,
                    "benefit": benefit,
                    "diploma": "any",
                    "grades": [10, 11],
                    "confirm_min_score": 75,
                    "programs": programs,
                    "note": years_note,
                }
                if subject.lower() != (profile or "").lower():
                    rule["confirm_subject"] = subject.lower()
                if benefit == "score100":
                    rule["score_for"] = f"ЕГЭ по предмету «{subject.lower()}»"
                rules.append(rule)
    return FaImport(rules=rules, rows=count)


def parse_fa_pdf(path) -> FaImport:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Нужен pdfplumber: py -m pip install pdfplumber") from exc
    with pdfplumber.open(path) as pdf:
        rows = [row for page in pdf.pages for table in page.extract_tables() for row in table]
    return parse_fa_rows(rows)
