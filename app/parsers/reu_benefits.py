"""Особые права РЭУ им. Плеханова (приложение 11 к правилам приёма, PDF).

Сначала таблица «номер → направление подготовки», затем по разделам «2025/2026 год», «2024/2025 год»…
льготы для дипломов соответствующего года перечня. Колонки:
    № | № в перечне | олимпиада | предметы | профиль | БВИ / 100 баллов | номера направлений | предмет ЕГЭ
Берём раздел самого свежего года (для дипломов прошлых лет условия бывают другими — это в примечании).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.benefits import VSOSH


@dataclass
class ReuImport:
    rules: list[dict]
    rows: int
    year: str = ""
    warnings: list[str] = field(default_factory=list)


def _clean(text: str | None) -> str | None:
    return None if text is None else re.sub(r"\s+", " ", text).strip()


def parse_reu_rows(rows: list[list[str | None]]) -> ReuImport:
    directions: dict[int, str] = {}
    section, sections_seen = None, 0
    prev = [""] * 8
    groups: dict[tuple, dict] = {}
    warnings, count = [], 0
    for raw in rows:
        cells = [_clean(c) for c in raw]
        if len(cells) == 2 and cells[0] and cells[0].isdigit() and cells[1]:
            directions[int(cells[0])] = cells[1]
            continue
        if len(cells) != 8:
            continue
        if cells[0] and re.match(r"20\d\d/20\d\d год", cells[0]):
            sections_seen += 1
            section = section or cells[0].removesuffix(" год")
            continue
        if sections_seen != 1 or (cells[5] or "").startswith("Тип"):
            continue  # только первый (самый свежий) раздел
        # None — объединённая ячейка, "" в первых колонках — продолжение блока на новой странице
        row = [prev[i] if c is None or (c == "" and i < 3) else c for i, c in enumerate(cells)]
        prev = row
        _, number, name, _, profile, kind, programs, confirm = row
        if not kind:
            continue
        count += 1
        benefit = "bvi" if kind.startswith("БВИ") else "score100" if kind.startswith("100") else None
        if benefit is None:
            warnings.append(f"не разобран тип льготы «{kind}» ({name})")
            continue
        if programs.lower().startswith("все"):
            program_names = None
        else:
            nums = [int(x) for x in re.findall(r"\d+", programs)]
            missing = [n for n in nums if n not in directions]
            if missing:
                warnings.append(f"{name[:40]}: нет направлений с номерами {missing}")
            program_names = [directions[n] for n in nums if n in directions]
        confirm = (confirm or "").replace("информатика и ИКТ", "информатика").strip()
        is_vsosh = name.lower().startswith("всероссийская олимпиада школьников") and "заключительный" in name.lower()
        rule = {
            "olympiad": VSOSH if is_vsosh else name,
            "rsr_number": int(number) if number and number.isdigit() and not is_vsosh else None,
            "profiles": [profile.lower()] if profile and profile != "-" else None,
            "benefit": benefit,
            "diploma": "any",
            "grades": None,
            "confirm_min_score": None if is_vsosh else 75,
        }
        if confirm and confirm != "-" and confirm != (profile or "").lower():
            rule["confirm_subject"] = confirm
        if benefit == "score100" and confirm and confirm != "-":
            rule["score_for"] = f"ЕГЭ по предмету «{confirm}»"
        key = repr(sorted(rule.items()))
        g = groups.setdefault(key, {**rule, "programs": [] if program_names is not None else None})
        if program_names is not None and g["programs"] is not None:
            g["programs"] += [p for p in program_names if p not in g["programs"]]
    return ReuImport(rules=list(groups.values()), rows=count, year=section or "", warnings=warnings)


def parse_reu_pdf(path) -> ReuImport:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Нужен pdfplumber: py -m pip install pdfplumber") from exc
    with pdfplumber.open(path) as pdf:
        rows = [row for page in pdf.pages for table in page.extract_tables() for row in table]
    return parse_reu_rows(rows)
