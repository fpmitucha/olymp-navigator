"""Особые права Сеченовского университета (приложение 5 к правилам приёма, PDF).

Университет даёт по олимпиадам РСОШ только «право на 100 баллов» (БВИ не даёт), победителям
и призёрам, за 10–11 класс, при ЕГЭ от 75 по предмету. Таблица: № | олимпиада | профиль |
вступительные испытания | предметы подтверждения | право. Строки таблицы разрезаны рамками
ячеек — их и используем (pdfplumber отдаёт границы как тонкие прямоугольники).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SUBJECTS = ["русский язык", "иностранный язык", "математика", "информатика", "физика", "химия",
            "биология", "история", "обществознание", "литература", "география"]


@dataclass
class SechenovImport:
    rules: list[dict]
    rows: int
    title: str = ""
    warnings: list[str] = field(default_factory=list)


def split_subjects(text: str) -> list[str]:
    """«информатика математика» → ["информатика", "математика"]."""
    text = text.lower().replace("ё", "е")
    found = [(text.find(s), s) for s in SUBJECTS if s in text]
    return [s for _, s in sorted(found)]


def _bands(rects, x0: float, x1: float) -> list[tuple[int, int]]:
    ys = sorted({round(r["top"]) for r in rects
                 if r["height"] < 1 and r["width"] > 20 and x0 - 3 <= r["x0"] <= x0 + 8 and r["x1"] >= x1 - 8})
    return list(zip(ys, ys[1:]))


def parse_pages(pages) -> SechenovImport:
    """pages — страницы pdfplumber. Колонки определяются по вертикальным рамкам шапки."""
    rules, warnings, count = [], [], 0
    current: tuple[str, str] | None = None
    for page in pages:
        xs = sorted({round(r["x0"]) for r in page.rects if r["width"] < 2 and r["height"] > 5})
        if len(xs) < 7:
            continue
        xs = xs[:7]

        def col(x: float) -> int | None:
            return next((i for i in range(6) if xs[i] - 3 <= x < xs[i + 1] - 3), None)

        words = sorted(page.extract_words(), key=lambda w: (round(w["top"]), w["x0"]))

        def text(band, c) -> str:
            return " ".join(w["text"] for w in words
                            if band[0] <= (w["top"] + w["bottom"]) / 2 < band[1] and col(w["x0"]) == c)

        olympiad_bands = _bands(page.rects, xs[1], xs[2])
        for band in _bands(page.rects, xs[2], xs[3]):
            profile, _, confirm, right = (text(band, c) for c in (2, 3, 4, 5))
            if not profile or profile.startswith("Профиль") or "100 баллов" not in right:
                continue
            ob = next((o for o in olympiad_bands if o[0] - 1 <= band[0] and band[1] <= o[1] + 1), None)
            if ob and re.match(r"\d+\.", text(ob, 0)):
                current = (text(ob, 0), text(ob, 1))
            if current is None:
                warnings.append(f"строка без олимпиады: {profile}")
                continue
            count += 1
            subjects = split_subjects(confirm)
            if not subjects:
                warnings.append(f"{current[1][:40]}: не разобран предмет «{confirm}»")
                continue
            name = re.sub(r"\s*\(.*\)\s*$", "", current[1]).replace('"', "«", 1)
            name = re.sub(r'"', "»", name, count=1) if "«" in name else name
            for subject in subjects:
                rules.append({
                    "olympiad": re.sub(r"\s+", " ", name).strip(),
                    "profiles": [profile.lower()],
                    "benefit": "score100",
                    "diploma": "any",
                    "grades": [10, 11],
                    "confirm_min_score": 75,
                    "programs": None,
                    "score_for": f"ЕГЭ по предмету «{subject}»",
                    **({"confirm_subject": subject} if subject != profile.lower() else {}),
                })
    return SechenovImport(rules=rules, rows=count, warnings=warnings)


def parse_sechenov_pdf(path) -> SechenovImport:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Нужен pdfplumber: py -m pip install pdfplumber") from exc
    with pdfplumber.open(path) as pdf:
        result = parse_pages(pdf.pages)
        first = pdf.pages[0].extract_text() or ""
    m = re.search(r"Перечень олимпиад школьников,.*?учебный год", first, re.S)
    result.title = re.sub(r"\s+", " ", m.group(0)) if m else ""
    return result
