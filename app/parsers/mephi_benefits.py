"""Особые права НИЯУ МИФИ со страницы «Победителям и призерам олимпиад» (HTML-таблица).

https://admission.mephi.ru/admission/baccalaureate-and-specialty/specials/winners

Первая таблица — Москва, колонки:
    № в перечне | олимпиада | уровень | направления БВИ | направления «100 баллов» | предмет | профиль

Общие условия со страницы: льготы победителям и призёрам; ЕГЭ (или экзамен МИФИ) от 75 по предмету;
БВИ — за 10 и 11 класс, 100 баллов — за 8–11 класс. Если предметов несколько («информатика/математика»),
направления перечислены через «/» в том же порядке.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

_LEVELS = {"I": 1, "II": 2, "III": 3}


@dataclass
class MephiImport:
    rules: list[dict]
    rows: int
    title: str = ""
    warnings: list[str] = field(default_factory=list)


def table_grid(table) -> list[list[str]]:
    """Таблица с rowspan/colspan → прямоугольная сетка текстов."""
    grid: list[list[str | None]] = []
    for ri, tr in enumerate(table.find_all("tr")):
        while len(grid) <= ri:
            grid.append([])
        ci = 0
        for td in tr.find_all(["td", "th"]):
            while ci < len(grid[ri]) and grid[ri][ci] is not None:
                ci += 1
            text = re.sub(r"\s+", " ", td.get_text(" ")).strip()
            rs, cs = int(td.get("rowspan", 1) or 1), int(td.get("colspan", 1) or 1)
            for r in range(rs):
                while len(grid) <= ri + r:
                    grid.append([])
                row = grid[ri + r]
                while len(row) < ci + cs:
                    row.append(None)
                for c in range(cs):
                    row[ci + c] = text
            ci += cs
    return [[c or "" for c in row] for row in grid]


def _programs(text: str) -> list[str]:
    """«01.03.02, 09.03.01» → коды; «все, кроме 41.03.05» — как есть."""
    text = text.strip(" /")
    if not text:
        return []
    if re.match(r"(?i)все", text):
        return [text[0].lower() + text[1:]]
    return re.findall(r"\d\d\.\d\d\.\d\d", text)


def parse_mephi_html(html: str) -> MephiImport:
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.find(string=re.compile(r"Особые права победителей и призеров олимпиад школьников, поступающих"))
    title = re.sub(r"\s+", " ", title_el).strip() if title_el else ""
    table = next((t for t in soup.find_all("table") if "Перечне олимпиад" in t.get_text()[:300]), None)
    if table is None:
        raise ValueError("На странице нет таблицы особых прав («№ в Перечне олимпиад»)")
    grid = table_grid(table)
    rules, warnings, rows = [], [], 0
    seen = set()
    for row in grid[1:]:
        if len(row) < 7:
            continue
        number, name, level, bvi, score, subject, profile = (c.strip() for c in row[:7])
        if not name or not profile:
            if name:
                warnings.append(f"без профиля: «{name}» ({subject})")
            continue
        rows += 1
        subjects = [s.strip() for s in subject.split("/") if s.strip()]
        levels = [_LEVELS[level]] if level in _LEVELS else None
        for kind, text, grades in (("bvi", bvi, [10, 11]), ("score100", score, [8, 9, 10, 11])):
            if not text.strip():
                continue
            parts = [p for p in text.split("/")]
            programs: list[str] = []
            for i, part in enumerate(parts):
                found = _programs(part)
                if len(subjects) > 1 and i < len(subjects) and found:
                    found = [f"{p} (по предмету «{subjects[i]}»)" if p.startswith("все") else p for p in found]
                programs += [p for p in found if p not in programs]
            rule = {
                "olympiad": name,
                "rsr_number": int(number) if number.isdigit() else None,
                "profiles": [profile.lower()],
                "levels": levels,
                "benefit": kind,
                "diploma": "any",
                "grades": grades,
                "confirm_min_score": 75,
                "programs": programs or None,
            }
            if subjects and subjects != [profile.lower()]:
                rule["confirm_subject"] = " или ".join(subjects)
            if kind == "score100" and subjects:
                rule["score_for"] = "ЕГЭ по предмету: " + " или ".join(subjects)
            key = repr(sorted(rule.items()))
            if key not in seen:  # в таблице МИФИ есть строки-дубли
                seen.add(key)
                rules.append(rule)
    return MephiImport(rules=rules, rows=rows, title=title, warnings=warnings)


def resolve_numbers(rules: list[dict], catalog, academic_year: str) -> list[dict]:
    """Номер в перечне надёжнее названия: подставляем точное название из каталога."""
    out = []
    for rule in rules:
        rule = dict(rule)
        number = rule.pop("rsr_number", None)
        found = catalog.by_number(academic_year, number) if number else None
        if found and _same(found.name, rule["olympiad"]):
            rule["olympiad"] = found.name
        out.append(rule)
    return out


def _same(a: str, b: str) -> bool:
    """Защита от опечаток в номере: названия должны пересекаться хотя бы по одному длинному слову."""
    words = lambda s: {w for w in re.findall(r"[а-яёa-z]{5,}", s.lower().replace("ё", "е"))}
    return bool(words(a) & words(b))
