"""Особые права РАНХиГС (www.ranepa.ru/bakalavriat/olimpiady/, документы приёма 2026).

* «Олимпиады_БВИ_Сводная <год>.xlsx», лист 3 — плоская таблица БВИ:
  программа | направление | предмет подтверждения | олимпиада | профили («a; b») | уровень образования.
* olimp-100ball-<год>.pdf — профиль олимпиады → предмет, за который ставят 100 баллов (только за 11 класс).
* olimp-vseross-<год>.pdf — профиль ВсОШ → направления для БВИ.

Общие условия со страницы вуза: БВИ — победителям и призёрам РСОШ за 10–11 класс, ЕГЭ от 75;
100 баллов — за 11 класс; ВсОШ — без условия по ЕГЭ.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.benefits import VSOSH
from app.parsers.xlsx import read_xlsx

_CODE = re.compile(r"\d\d\.\d\d\.\d\d")


@dataclass
class RanepaImport:
    rules: list[dict]
    rows: int
    warnings: list[str] = field(default_factory=list)


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _name(text: str) -> str:
    """Кавычки "…" → «…» (так названия в каталоге)."""
    text = _clean(text)
    while '"' in text:
        text = text.replace('"', "«", 1).replace('"', "»", 1)
    return text


def parse_bvi_xlsx(data: bytes) -> RanepaImport:
    rows = read_xlsx(data, sheet=3)
    header = next(i for i, r in enumerate(rows) if r.get("A", "").startswith("Образовательная программа"))
    per_program: dict[tuple, set[str]] = {}
    count = 0
    for r in rows[header + 1:]:
        program, direction, confirm, olympiad, profiles, level = (_clean(r.get(k)) for k in "ABCDEF")
        if not olympiad or not profiles or (level and level not in ("Бакалавриат", "Специалитет")):
            continue
        count += 1
        for profile in filter(None, (p.strip().lower() for p in profiles.split(";"))):
            key = (_name(olympiad), profile, f"{direction} — {program}")
            per_program.setdefault(key, set()).add(confirm.lower())
    groups: dict[tuple, dict] = {}
    for (olympiad, profile, program), confirms in per_program.items():
        confirm = " или ".join(sorted(c for c in confirms if c))
        key = (olympiad, profile, confirm)
        g = groups.setdefault(key, {
            "olympiad": olympiad, "profiles": [profile], "benefit": "bvi", "diploma": "any",
            "grades": [10, 11], "confirm_min_score": 75, "programs": [],
            **({"confirm_subject": confirm} if confirm and confirm != profile else {}),
        })
        g["programs"].append(program)
    for g in groups.values():
        g["programs"].sort()
    return RanepaImport(rules=list(groups.values()), rows=count)


def parse_100_rows(rows: list[list[str | None]]) -> RanepaImport:
    prev = [""] * 4
    groups: dict[tuple, dict] = {}
    count = 0
    for raw in rows:
        if len(raw) != 4:
            continue
        cells = [None if c is None else _clean(c) for c in raw]
        if cells[0] in ("№ п/п.", "1") and cells[1] in ("Полное наименование олимпиады", "2"):
            continue
        row = [prev[i] if c is None else c for i, c in enumerate(cells)]
        prev = row
        _, olympiad, profile, subject = row
        if not olympiad or not profile or not subject:
            continue
        count += 1
        key = (_name(olympiad), profile.lower())
        g = groups.setdefault(key, {
            "olympiad": _name(olympiad), "profiles": [profile.lower()], "benefit": "score100",
            "diploma": "any", "grades": [11], "confirm_min_score": 75, "programs": None, "_subjects": [],
        })
        if subject.lower() not in g["_subjects"]:
            g["_subjects"].append(subject.lower())
    rules = []
    for g in groups.values():
        subjects = g.pop("_subjects")
        g["score_for"] = "ЕГЭ по предмету «" + "» или «".join(subjects) + "»"
        if subjects != g["profiles"]:
            g["confirm_subject"] = " или ".join(subjects)
        rules.append(g)
    return RanepaImport(rules=rules, rows=count)


def parse_vsosh_rows(rows: list[list[str | None]], vsosh_profiles: list[str]) -> RanepaImport:
    """«Английский язык» | «БАКАЛАВРИАТ 01.03.02 … СПЕЦИАЛИТЕТ …» (ячейка программ объединена на несколько строк)."""
    rules, warnings, count = [], [], 0
    prev_programs = ""
    for raw in rows:
        if len(raw) != 3 or not raw[1] or raw[1].startswith("Общеобразовательный") or raw[1] == "2":
            continue
        subject = _clean(raw[1]).lower()
        programs_text = _clean(raw[2]) if raw[2] else prev_programs
        prev_programs = programs_text
        count += 1
        profile = subject if subject in vsosh_profiles else next(
            (p for p in vsosh_profiles if p.split(" ")[0] == subject.split(" ")[0]), None)
        if profile is None:
            warnings.append(f"ВсОШ: не найден предмет «{subject}»")
            continue
        starts = [m.start() for m in _CODE.finditer(programs_text)]
        programs = [re.sub(r"\s*(БАКАЛАВРИАТ|СПЕЦИАЛИТЕТ)\s*$", "", programs_text[a:b]).strip(" ,;")
                    for a, b in zip(starts, starts[1:] + [len(programs_text)])]
        rules.append({"olympiad": VSOSH, "profiles": [profile], "benefit": "bvi", "diploma": "any",
                      "grades": None, "confirm_min_score": None, "programs": programs or None})
    return RanepaImport(rules=rules, rows=count, warnings=warnings)


def read_pdf_rows(path) -> list[list[str | None]]:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Нужен pdfplumber: py -m pip install pdfplumber") from exc
    with pdfplumber.open(path) as pdf:
        return [row for page in pdf.pages for table in page.extract_tables() for row in table]
