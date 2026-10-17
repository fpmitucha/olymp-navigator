"""Особые права ВШЭ из приложений к «Порядку учёта результатов победителей и призёров олимпиад».

Таблица (проверено на приложении 2 «НИУ ВШЭ (г. Москва) для поступающих в 2026 году»):

    A  № п/п или «Направление подготовки 01.03.01 Математика»
    B  наименование образовательной программы
    C  полное наименование олимпиады
    D  профиль олимпиады          E  предмет олимпиады
    F  предметы ЕГЭ для подтверждения
    G  балл ЕГЭ «75 и более» (или «-» для творческих программ)
    H  «Право на прием БВИ» / «Право на 100 баллов»
    I  предмет зачёта 100 баллов
    J  «Победителям» / «Победителям и призерам»
    K  «11 класс» / «10 класс» / «10, 11 класс»

Программа и олимпиада указаны один раз на блок строк. В строке «Право на 100 баллов»,
идущей под строкой БВИ того же профиля, профиль и балл часто пустые — они берутся
из строки выше. Строки «Все олимпиады, входящие в Перечень» пропускаются (общее правило,
в боте его пока не показываем) — их число возвращается в отчёте.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.parsers.xlsx import read_xlsx

ALL_OLYMPIADS = "все олимпиады"


@dataclass
class HseImport:
    rules: list[dict]
    rows: int
    skipped_generic: int
    programs: int
    title: str = ""
    warnings: list[str] = field(default_factory=list)


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _grades(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", text.replace("класс", ""))]


def parse_hse_table(data: bytes) -> HseImport:
    rows = read_xlsx(data)
    header = next((i for i, r in enumerate(rows) if "Наименование образовательной программы" in r.get("B", "")), None)
    if header is None:
        raise ValueError("Не найдена строка заголовков («Наименование образовательной программы» в колонке B)")
    title = next((_clean(r.get("A")) for r in rows[:header] if "Особые права" in r.get("A", "")), "")

    direction = program = olympiad = ""
    prev: dict[str, str] = {}
    parsed = []
    skipped_generic = 0
    for r in rows[header + 1:]:
        a, b = _clean(r.get("A")), _clean(r.get("B"))
        if re.match(r"^(Направление подготовки|Специальность)", a):
            direction = re.sub(r"^(Направление подготовки|Специальность)\s*", "", a)
            continue
        if b:
            program, olympiad, prev = b, "", {}
        if _clean(r.get("C")):
            olympiad, prev = _clean(r.get("C")), {}
        right = _clean(r.get("H"))
        if not right:
            continue
        cur = {k: _clean(r.get(k)) for k in "DFGIJK"}
        for k, v in cur.items():
            if not v:
                cur[k] = prev.get(k, "")
        prev = cur
        if ALL_OLYMPIADS in olympiad.lower():
            skipped_generic += 1
            continue
        score = re.search(r"\d+", cur["G"])
        code = re.match(r"[\d.]+", direction)
        parsed.append({
            "olympiad": olympiad,
            "profile": cur["D"].lower(),
            "confirm": cur["F"].lower(),
            "score": int(score.group(0)) if score else None,
            "benefit": "bvi" if "БВИ" in right else "score100",
            "diploma": "any" if re.search(r"призер", cur["J"], re.IGNORECASE) else "winner",
            "grades": _grades(cur["K"]),
            "program": f"{code.group(0) + ' ' if code else ''}{program}",
        })

    groups: dict[tuple, dict] = {}
    for p in parsed:
        key = (p["olympiad"], p["profile"], p["benefit"], p["diploma"], tuple(p["grades"]), p["score"], p["confirm"])
        g = groups.setdefault(key, {**p, "programs": []})
        if p["program"] not in g["programs"]:
            g["programs"].append(p["program"])

    rules = []
    for g in groups.values():
        rule = {
            "olympiad": g["olympiad"],
            "profiles": [g["profile"]] if g["profile"] else None,
            "benefit": g["benefit"],
            "diploma": g["diploma"],
            "grades": g["grades"] or None,
            "confirm_min_score": g["score"],
            "programs": g["programs"],
        }
        if g["confirm"] and g["confirm"] != g["profile"]:
            rule["confirm_subject"] = g["confirm"]
        rules.append(rule)
    programs = len({p["program"] for p in parsed})
    return HseImport(rules=rules, rows=len(parsed), skipped_generic=skipped_generic, programs=programs, title=title)
