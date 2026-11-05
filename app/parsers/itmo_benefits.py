"""Особые права ИТМО из трёх PDF (abit.itmo.ru/file_storage/file/pages/82/…):

* rsosh_bvi_<год>.pdf — олимпиады РСОШ, дающие БВИ (дипломы 10–11 класса);
* rsosh_100_<год>.pdf — олимпиады РСОШ, дающие 100 баллов за ВИ;
* vsosh_<год>.pdf — профили ВсОШ и направления для БВИ.

Таблицы РСОШ: направления | олимпиада («Все из перечня…» — любая) | профиль | предмет ЕГЭ от 75 |
уровень («1, 2 или 3») | диплом. Направления и олимпиада указаны один раз на блок (объединённые ячейки).

В узких колонках слова переносятся без дефиса («Информат\\nика») — склеиваем такие куски,
если получается известное слово (словарь — все слова документа и каталога).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.benefits import VSOSH, WILDCARD

_STOP = {"и", "в", "с", "к", "о", "у", "а", "по", "на", "для", "из", "или", "и(или)"}
_CODE = re.compile(r"\d\d\.\d\d\.\d\d")


@dataclass
class ItmoImport:
    rules: list[dict]
    rows: int
    warnings: list[str] = field(default_factory=list)


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[А-Яа-яЁё]+", text)}


class Joiner:
    """Склеивает строки ячейки: «Информат\\nика» → «Информатика», «Экология\\nи» → «Экология и»."""

    def __init__(self, vocabulary: set[str]):
        self.vocabulary = vocabulary

    def __call__(self, text: str | None) -> str:
        if not text:
            return ""
        lines = [x.strip() for x in text.split("\n") if x.strip()]
        out = lines[0] if lines else ""
        for line in lines[1:]:
            head = line.split(" ")[0]
            tail = re.findall(r"[А-Яа-яЁё]+$", out)
            glued = (tail[0] + re.match(r"[А-Яа-яЁё]*", head).group(0)).lower() if tail else ""
            if out.endswith("-") and head[:1].islower() and head.lower() not in _STOP:
                out += line  # «Бизнес-\\nинформатика»
            elif tail and head[:1].islower() and head.lower() not in _STOP and (
                    glued in self.vocabulary
                    or len(head.rstrip(",.;")) <= 3 and head.rstrip(",.;").lower() not in self.vocabulary):
                out += line
            else:
                out += " " + line
        return re.sub(r"\s+", " ", out).strip()


def _levels(text: str) -> list[int] | None:
    found = sorted({int(x) for x in re.findall(r"[123]", text)})
    return found or None


def _programs(text: str) -> list[str]:
    """«01.03.02 Прикладная математика, 09.03.02 Информационные…» → по направлению на элемент."""
    starts = [m.start() for m in _CODE.finditer(text)]
    return [text[a:b].strip(" ,;") for a, b in zip(starts, starts[1:] + [len(text)])]


def parse_rsosh_rows(rows: list[list[str | None]], benefit: str, join: Joiner) -> ItmoImport:
    prev = [""] * 6
    groups: dict[tuple, dict] = {}
    warnings, count = [], 0
    for raw in rows:
        if len(raw) != 6:
            continue
        cells = [None if c is None else join(c) for c in raw]
        if cells[5] in ("молпиД", "Диплом") or (cells[4] or "").startswith("ыдаипмило"):
            continue  # шапка (в PDF она перевёрнута)
        row = [prev[i] if c is None or (c == "" and i in (0, 1)) else c for i, c in enumerate(cells)]
        prev = row
        directions, olympiad, profile, subject, level, diploma = row
        if not profile or not diploma:
            continue
        count += 1
        restriction = re.search(r"\((.*?)\)?\s*$", profile) if "(" in profile else None
        profile = re.sub(r"\(.*$", "", profile).strip()
        dip = "any" if "призер" in diploma.lower().replace("ё", "е") else "winner"
        subject = re.sub(r"\s+", "", subject).lower()  # «Обществоз нание»
        subject = {"обществознанию": "обществознание", "иностранныйязык": "иностранный язык",
                   "русскийязык": "русский язык"}.get(subject, subject)
        rule = {
            "olympiad": WILDCARD if olympiad.lower().startswith("все из перечня") else olympiad,
            "profiles": [x.strip() for x in profile.lower().split("/") if x.strip()],
            "levels": _levels(level),
            "benefit": benefit,
            "diploma": dip,
            "grades": [10, 11],
            "confirm_min_score": 75,
        }
        if restriction:
            rule["note"] = restriction.group(1).strip(" )").replace("  ", " ")
        if subject and subject != profile.lower():
            rule["confirm_subject"] = subject
        if benefit == "score100" and subject:
            rule["score_for"] = f"ВИ по предмету «{subject}»"
        key = repr(sorted(rule.items()))
        g = groups.setdefault(key, {**rule, "programs": []})
        for p in _programs(directions):
            if p not in g["programs"]:
                g["programs"].append(p)
    return ItmoImport(rules=list(groups.values()), rows=count, warnings=warnings)


def parse_vsosh_rows(rows: list[list[str | None]], join: Joiner, vsosh_profiles: list[str]) -> ItmoImport:
    """«Информатика (Искусственный интеллект…)», «Химия, Биология, Экология, Технология» → профили ВсОШ."""
    by_first_word = {p.split(" ")[0].lower(): p for p in vsosh_profiles}
    rules, warnings, count = [], [], 0
    for raw in rows:
        if len(raw) != 2 or not raw[0] or not _CODE.search(raw[0] or ""):
            continue
        directions, subjects = join(raw[0]), join(raw[1])
        if not subjects:
            continue
        count += 1
        bare = subjects
        while re.search(r"\([^()]*\)", bare):  # «Технология (Робототехника и(или) …)» — скобки вложенные
            bare = re.sub(r"\([^()]*\)", "", bare)
        names = [x.strip().lower() for x in bare.split(",")]
        profiles = []
        for name in filter(None, names):
            match = name if name in vsosh_profiles else by_first_word.get(name.split(" ")[0])
            if match is None and name == "технология":
                match = next((p for p in vsosh_profiles if "технология" in p), None)
            if match:
                profiles.append(match)
            else:
                warnings.append(f"ВсОШ: не найден предмет «{name}»")
        if profiles:
            note = re.search(r"\((.*)\)", subjects)
            rules.append({
                "olympiad": VSOSH, "profiles": sorted(set(profiles)), "benefit": "bvi", "diploma": "any",
                "grades": None, "confirm_min_score": None, "programs": _programs(directions),
                **({"note": f"по профилю: {note.group(1)}"} if note else {}),
            })
    return ItmoImport(rules=rules, rows=count, warnings=warnings)


def read_pdf_rows(path) -> tuple[list[list[str | None]], str]:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Нужен pdfplumber: py -m pip install pdfplumber") from exc
    with pdfplumber.open(path) as pdf:
        rows = [row for page in pdf.pages for table in page.extract_tables() for row in table]
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return rows, text


def build_joiner(texts: list[str], catalog) -> Joiner:
    vocabulary: set[str] = set()
    for t in texts:
        vocabulary |= _words(t.replace("\n", " "))
    for o in catalog.olympiads.values():
        vocabulary |= _words(o.name)
        for name, by_year in o.profiles.items():
            vocabulary |= _words(name)
            for p in by_year.values():
                vocabulary |= _words(" ".join(p.subjects))
    return Joiner(vocabulary)
