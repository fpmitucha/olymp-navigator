"""Сохранение разобранного перечня: JSON (для загрузки в базу) и CSV (чтобы смотреть глазами)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from app.parsers.rsr import RsrList


def year_slug(academic_year: str) -> str:
    return academic_year.replace("/", "-")


def save_json(rsr: RsrList, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rsr.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_json(path: Path) -> RsrList:
    return RsrList.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_csv(rsr: RsrList, path: Path) -> Path:
    """Одна строка на профиль. Кодировка utf-8-sig, чтобы Excel открыл кириллицу."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["учебный год", "№", "олимпиада", "профиль", "уровень", "предметы", "сайт"])
        for olympiad in rsr.olympiads:
            for p in olympiad.profiles:
                writer.writerow([
                    rsr.academic_year,
                    olympiad.number or "",
                    olympiad.name,
                    p.profile,
                    p.level or "",
                    " | ".join(p.subjects),
                    p.website or "",
                ])
    return path
