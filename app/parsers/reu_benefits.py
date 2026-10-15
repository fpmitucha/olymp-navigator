"""Особые права РЭУ им. Плеханова: приложение 11 к правилам приёма
«Перечень олимпиад школьников, по которым… устанавливаются льготы».

В начале — нумерованный список направлений (1 — Прикладная математика и информатика, …).
Дальше разделы по учебным годам олимпиады («2025/2026 год», «2024/2025 год», …), в каждом таблица:
    № | № в перечне | олимпиада | предметы | профиль | БВИ / 100 баллов | номера направлений | предмет ЕГЭ
Строки со звёздочкой («БВИ*») в конце раздела — всероссийская олимпиада (ВсОШ) по предмету.
Берём раздел нужного учебного года (по умолчанию самый свежий).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.benefits import VSOSH

YEAR = re.compile(r"^(\d{4})/(\d{4}) год$")


@dataclass
class ReuImport:
    rules: list[dict]
    rows: int
    year: str = ""
    directions: dict[int, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    return re.sub(r"(\w)- (\w)", r"\1-\2", re.sub(r"\s+", " ", text)).strip()


def parse_reu_rows(rows: list[list[str | None]], year: str | None = None) -> ReuImport:
    directions: dict[int, str] = {}
    sections: dict[str, list[list[str]]] = {}
    current_year = None
    for raw in rows:
        cells = [_clean(c) for c in raw]
        if len(cells) == 2 and cells[0] and cells[0].isdigit() and not sections:
            directions[int(cells[0])] = cells[1] or ""
            continue
        if len(cells) != 8:
            continue
        if cells[0] and (m := YEAR.match(cells[0])):
            current_year = f"{m.group(1)}/{m.group(2)[2:]}"
            sections.setdefault(current_year, [])
            continue
        if current_year is None or (cells[5] or "").startswith("Тип"):
            continue
        sections[current_year].append(cells)
    if not sections:
        raise ValueError("В документе не нашлось разделов «ГГГГ/ГГГГ год»")
    year = year or max(sections)
    names: dict[str, str] = {}
    prev = [""] * 8
    groups: dict[tuple, dict] = {}
    warnings, count = [], 0
    for cells in sections[year]:
        row = [prev[i] if c is None or (c == "" and i < 3) else c for i, c in enumerate(cells)]
        if cells[1] and cells[2]:
            names[cells[1]] = cells[2]
        elif cells[1] and cells[2] is None:
            row[2] = names.get(cells[1], "")  # продолжение на новой странице; если имени нет — найдём по номеру
        prev = row
        _, number, name, subjects, profile, kind, dirs, confirm = row
        if not kind:
            continue
        count += 1
        is_vsosh = kind.endswith("*")
        benefit = "bvi" if kind.rstrip("*") == "БВИ" else "score100" if kind.startswith("100") else None
        if benefit is None:
            warnings.append(f"не разобрана льгота «{kind}» ({name})")
            continue
        if re.search(r"все направлени", dirs or "", re.I):
            programs = None
        else:
            programs = [directions.get(int(n), f"направление №{n}") for n in re.findall(r"\d+", dirs or "")]
        confirm_l = (confirm or "").lower().replace("информатика и икт", "информатика")
        rule = {
            "olympiad": VSOSH if is_vsosh else name,
            "rsr_number": None if is_vsosh else (int(number) if number and number.isdigit() else None),
            "profiles": [profile.lower()] if profile else None,
            "benefit": benefit,
            "diploma": "any",
            "confirm_min_score": None if is_vsosh or confirm_l in ("", "-") else 75,
            "programs": programs,
        }
        if confirm_l not in ("", "-") and confirm_l != (profile or "").lower():
            rule["confirm_subject"] = confirm_l
        if benefit == "score100" and confirm_l not in ("", "-"):
            rule["score_for"] = f"ЕГЭ по предмету «{confirm_l}»"
        key = repr(sorted((k, v) for k, v in rule.items() if k != "programs"))
        g = groups.setdefault(key, {**rule, "programs": [] if programs is not None else None})
        if programs is None:
            g["programs"] = None
        elif g["programs"] is not None:
            g["programs"] += [p for p in programs if p not in g["programs"]]
    return ReuImport(rules=list(groups.values()), rows=count, year=year, directions=directions, warnings=warnings)


def parse_reu_pdf(path, year: str | None = None) -> ReuImport:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Нужен pdfplumber: py -m pip install pdfplumber") from exc
    rows: list[list[str | None]] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.find_tables():
                rows += _recover_lost_names(page, table, table.extract())
    return parse_reu_rows(rows, year)


def _recover_lost_names(page, table, rows: list[list[str | None]]) -> list[list[str | None]]:
    """Начало олимпиады у верхнего края страницы pdfplumber иногда теряет (ячейка без верхней рамки):
    номер и название есть на странице, а в таблице — None. Достаём их по координатам строки."""
    if not rows or len(rows[0]) != 8:
        return rows
    full = next((r for r in table.rows if all(c is not None for c in r.cells)), None)
    if full is None:
        return rows
    columns = [(c[0], c[2]) for c in full.cells[:3]]
    out, recovered = [], None
    for row, cells in zip(table.rows, rows):
        cells = list(cells)
        if not cells[0] and not cells[1] and not cells[2]:
            top, bottom = row.bbox[1], row.bbox[3]
            texts = [(page.crop((x0, top, x1, bottom)).extract_text() or "").strip() for x0, x1 in columns]
            if texts[1].isdigit() or texts[2]:
                if recovered is not None and (not texts[1].isdigit() or not recovered[1] or recovered[1] == texts[1]):
                    recovered[2] = f"{recovered[2]} {texts[2]}".strip()  # продолжение названия
                    recovered[0] = recovered[0] or texts[0]
                    recovered[1] = recovered[1] or (texts[1] if texts[1].isdigit() else "")
                else:
                    cells[0], cells[1], cells[2] = texts[0], texts[1] if texts[1].isdigit() else "", texts[2]
                    recovered = cells
            elif recovered is not None:
                pass  # строки того же блока без текста
        else:
            recovered = None
        out.append(cells)
    return out
