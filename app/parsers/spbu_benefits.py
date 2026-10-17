"""Особые права СПбГУ из PDF «Перечень олимпиад школьников… особые права при приеме в СПбГУ»
(abiturient.spbu.ru/medialibrary/ru/<год>/bac/bac_spec_olymp_2_<год>.pdf).

Колонки таблицы (pdfplumber, объединённые ячейки — None):
    0 направление  1 программа  2 форма  3 № ВИ  4 предмет ВИ  5 мин. балл  6 уровень олимпиады
    7 олимпиада («—» — любая)  8 профиль («—» — любой)  9 предмет / укрупнённая группа
    10 победитель/призёр  11 льгота: «Без вступительных испытаний» / «100 баллов за ВИ по …»

«—» в колонке олимпиады — любая олимпиада перечня этого уровня: с указанным профилем,
а если и профиль «—», то с указанным предметом (по перечню Минобрнауки).
Дипломы засчитываются только за 10 и 11 класс.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.benefits import WILDCARD

DASH = "—"
_LEVELS = {"I": 1, "II": 2, "III": 3}
_FIXES = {"социология и социальная": "социология и социальная работа"}


@dataclass
class SpbuImport:
    rules: list[dict]
    rows: int
    title: str = ""
    warnings: list[str] = field(default_factory=list)


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"(\w)- (\w)", r"\1-\2", text).replace("ѐ", "ё")


def parse_levels(text: str) -> list[int]:
    """«I–III» → [1, 2, 3], «II» → [2]."""
    found = [_LEVELS[x] for x in re.findall(r"I{1,3}", text) if x in _LEVELS]
    if not found:
        return []
    if re.search(r"[–-]", text) and len(found) == 2:
        return list(range(found[0], found[1] + 1))
    return sorted(set(found))


def parse_diploma(text: str) -> str | None:
    t = text.lower().replace("ё", "е")
    winner, prize = "победител" in t, "призер" in t
    return "any" if winner and prize else "winner" if winner else "prize" if prize else None


def parse_spbu_rows(rows: list[list[str | None]]) -> SpbuImport:
    prev: list[str] = [""] * 12
    groups: dict[tuple, dict] = {}
    warnings: list[str] = []
    count = 0
    for raw in rows:
        if len(raw) < 12:
            continue
        cells = [_clean(c) for c in raw[:12]]
        if cells[6] in ("Уровень олимпиады",) or (cells[6] or "").startswith("Особые права не"):
            if cells[6] and cells[6].startswith("Особые права не"):
                prev = [prev[i] if c is None else c for i, c in enumerate(cells)]
            continue
        if cells[0] and cells[1] is None and not any(cells[2:]):
            continue  # «01.00.00 МАТЕМАТИКА И МЕХАНИКА — БАКАЛАВРИАТ»
        row = [prev[i] if c is None else c for i, c in enumerate(cells)]
        prev = row
        direction, program, form, _, exam, min_score, level, olympiad, profile, subject, category, benefit = row
        if not benefit or not level:
            continue
        diploma = parse_diploma(category)
        if benefit.startswith("Без вступительных"):
            kind, score_for = "bvi", None
        elif m := re.match(r"100 баллов за ВИ (?:по )?(.+)", benefit):
            kind, score_for = "score100", f"ВИ по {m.group(1)}"
        else:
            warnings.append(f"не разобрана льгота «{benefit}» ({direction})")
            continue
        if diploma is None:
            warnings.append(f"не разобрано «{category}» ({direction})")
            continue
        count += 1
        prog = f"{direction} — {program}" + (f" ({form})" if form and form != "очная" else "")
        profile_l = (profile or DASH).lower()
        subject_l = _FIXES.get((subject or DASH).lower(), (subject or DASH).lower())
        if subject_l.startswith("работа "):  # хвост «Социология и социальная | работа» уехал в следующую строку
            subject_l = subject_l.removeprefix("работа ")
        rule = {
            "olympiad": olympiad if olympiad and olympiad != DASH else WILDCARD,
            "profiles": [profile_l] if profile_l != DASH else None,
            "levels": parse_levels(level) or None,
            "benefit": kind,
            "diploma": diploma,
            "grades": [10, 11],
            "confirm_min_score": int(min_score) if min_score and min_score.isdigit() else None,
        }
        if rule["olympiad"] == WILDCARD and not rule["profiles"]:
            if subject_l == DASH:
                warnings.append(f"«—» без профиля и предмета ({direction})")
                continue
            rule["subjects"] = [subject_l]
        if exam and exam.lower() not in (profile_l, subject_l):
            rule["confirm_subject"] = exam.lower()
        if score_for:
            rule["score_for"] = score_for
        key = repr(sorted(rule.items()))
        g = groups.setdefault(key, {**rule, "programs": []})
        if prog not in g["programs"]:
            g["programs"].append(prog)
    return SpbuImport(rules=list(groups.values()), rows=count, warnings=warnings)


def parse_spbu_pdf(path) -> SpbuImport:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Нужен pdfplumber: py -m pip install pdfplumber") from exc
    with pdfplumber.open(path) as pdf:
        rows = [row for page in pdf.pages for table in page.extract_tables() for row in table]
        first = pdf.pages[0].extract_text() or ""
    result = parse_spbu_rows(rows)
    result.title = re.sub(r"\s+", " ", first.split("Особые права предоставляются")[0]).strip()
    return result
