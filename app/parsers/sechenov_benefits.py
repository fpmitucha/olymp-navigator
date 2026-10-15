"""Особые права Сеченовского университета: приложение 5 к правилам приёма «Перечень олимпиад школьников…».

Университет даёт только право на 100 баллов (БВИ по олимпиадам школьников не даёт), дипломы за 10–11 класс,
подтверждение — ЕГЭ или внутреннее испытание от 75 баллов.

Таблица без рамок, ячейки переносятся на несколько строк, поэтому читаем слова с координатами (pdfplumber)
и раскладываем по колонкам по x: № | олимпиада | профиль | испытание, за которое 100 баллов |
предмет подтверждения | «право на 100 баллов». Строка данных — та, где в последней колонке «право».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

COLUMNS = (("num", 0), ("name", 110), ("profile", 295), ("exam", 485), ("confirm", 583), ("right", 680))
HEADER = re.compile(r"^(№|Полное|Профиль|Общеобразовательные|Предмет|Предоставляемое)")


@dataclass
class SechenovImport:
    rules: list[dict]
    rows: int
    title: str = ""
    warnings: list[str] = field(default_factory=list)


def _column(x: float) -> str:
    name = COLUMNS[0][0]
    for col, start in COLUMNS:
        if x >= start:
            name = col
    return name


def lines_from_words(words: list[dict]) -> list[dict[str, str]]:
    """Слова одной страницы → строки {колонка: текст}, сверху вниз."""
    rows: dict[int, dict[str, list[str]]] = {}
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        key = next((k for k in rows if abs(k - w["top"]) <= 3), round(w["top"]))
        rows.setdefault(key, {}).setdefault(_column(w["x0"]), []).append(w["text"])
    return [{c: " ".join(t) for c, t in rows[k].items()} for k in sorted(rows)]


def parse_lines(lines: list[dict[str, str]]) -> SechenovImport:
    """Собирает олимпиады: название и профиль могут занимать несколько строк."""
    records: list[dict] = []    # {"name": [...], "entries": [{"profile": [...], "exam": .., "confirm": ..}]}
    current: dict | None = None
    in_table = header = False
    for line in lines:
        text = " ".join(line.values())
        if HEADER.match(line.get("num", "") + line.get("name", "")):
            in_table = header = True   # шапка повторяется на каждой странице и занимает много строк
            continue
        if header:
            header = "баллов)" not in line.get("confirm", "")
            continue
        if not in_table or text.startswith("*") or "Приказ Минобрнауки" in text:
            continue
        num = line.get("num", "")
        if re.fullmatch(r"\d+\.", num):
            current = {"name": [], "entries": []}
            records.append(current)
        if current is None:
            continue
        if line.get("name"):
            current["name"].append(line["name"])
        is_data = line.get("right", "").startswith("право")
        if is_data:
            entry = {"profile": [line["profile"]] if line.get("profile") else None,
                     "exam": line.get("exam", ""), "confirm": line.get("confirm", "")}
            if entry["profile"] is None and current["entries"]:
                entry["profile"] = current["entries"][-1]["profile"]   # тот же профиль, другой предмет
            current["entries"].append(entry)
        elif current["entries"]:  # перенос текста ячеек на следующую строку
            last = current["entries"][-1]
            if line.get("profile"):
                last["profile"].append(line["profile"])
            for col in ("exam", "confirm"):
                if line.get(col):
                    last[col] += " " + line[col]
    rules, warnings, count = [], [], 0
    for rec in records:
        name = re.sub(r"\s+", " ", " ".join(rec["name"])).strip()
        name = re.sub(r"(\w)- (\w)", r"\1-\2", name)
        for e in rec["entries"]:
            count += 1
            if not e["profile"]:
                warnings.append(f"без профиля: «{name}»")
                continue
            profile = re.sub(r"(\w)- (\w)", r"\1-\2", " ".join(e["profile"])).strip().lower()
            exam, confirm = e["exam"].strip().lower(), e["confirm"].strip().lower()
            rule = {"olympiad": name, "profiles": [profile], "benefit": "score100", "diploma": "any",
                    "grades": [10, 11], "confirm_min_score": 75, "programs": None,
                    "score_for": f"ЕГЭ по предмету «{exam}»" if exam else None}
            if confirm and confirm != profile:
                rule["confirm_subject"] = confirm
            if rule not in rules:
                rules.append(rule)
    return SechenovImport(rules=rules, rows=count, warnings=warnings)


def parse_sechenov_pdf(path) -> SechenovImport:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Нужен pdfplumber: py -m pip install pdfplumber") from exc
    with pdfplumber.open(path) as pdf:
        lines = [line for page in pdf.pages for line in lines_from_words(page.extract_words())]
        first = pdf.pages[0].extract_text() or ""
    result = parse_lines(lines)
    m = re.search(r"Перечень олимпиад школьников,.*?учебный год", first, re.S)
    result.title = re.sub(r"\s+", " ", m.group(0)) if m else ""
    return result
