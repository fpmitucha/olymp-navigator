"""Льготы вузов для победителей и призёров олимпиад.

Данные — JSON-файлы в папке benefits/ (один файл = один вуз и один год приёма), их собирают
и проверяют вручную по официальным документам вузов. Формат описан в benefits/README.md.

Правило: олимпиада (+ профили, уровни) → какая льгота (БВИ или 100 баллов), кому (победителям,
призёрам), на какие направления, при каком ЕГЭ. Общие условия вуза задаются в `defaults`
и действуют для всех правил, пока правило не переопределит их.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.catalog import Catalog, CatalogOlympiad

BENEFIT_TYPES = {"bvi": "БВИ", "score100": "100 баллов"}
DIPLOMAS = {"winner": "победителям", "prize": "призёрам", "any": "победителям и призёрам"}
DOCUMENT_STATUSES = {"draft": "проект", "approved": "утверждено"}
VSOSH = "ВсОШ"


class BenefitsError(ValueError):
    pass


@dataclass
class Rule:
    olympiad_query: str            # как записано в файле — для сообщений об ошибках
    olympiad: CatalogOlympiad
    benefit: str                   # bvi | score100
    diploma: str                   # winner | prize | any
    profiles: list[str] | None     # None — любой профиль (или вуз ещё не уточнил)
    levels: list[int] | None       # None — любой уровень
    programs: list[str] | None     # None — все направления, соответствующие профилю
    confirm_min_score: int | None  # минимальный балл подтверждающего ЕГЭ
    confirm_subject: str | None    # предмет; None — «соответствующий профилю олимпиады»
    grades: list[int] | None       # за какой класс засчитывается диплом
    diploma_since: int | None      # диплом не раньше этого года
    note: str | None


@dataclass
class UniversityBenefits:
    university: str                # короткое имя: «МФТИ»
    full_name: str
    city: str
    admission_year: int            # 2027 — приём летом 2027
    document_status: str           # draft | approved
    source_url: str
    source_title: str
    checked_at: date
    notes: list[str]
    rules: list[Rule]
    file: str = ""

    @property
    def is_draft(self) -> bool:
        return self.document_status == "draft"

    def status_label(self, current_admission_year: int) -> str:
        """«приём 2027, проект», «приём 2026 — прошлый год»."""
        label = f"приём {self.admission_year}"
        if self.admission_year < current_admission_year:
            return label + " — прошлый год"
        if self.is_draft:
            return label + ", проект"
        return label


@dataclass
class BenefitsCatalog:
    universities: list[UniversityBenefits] = field(default_factory=list)

    def latest(self) -> list[UniversityBenefits]:
        """По каждому вузу — данные самого свежего года приёма."""
        best: dict[str, UniversityBenefits] = {}
        for u in self.universities:
            if u.university not in best or u.admission_year > best[u.university].admission_year:
                best[u.university] = u
        return sorted(best.values(), key=lambda u: u.university)

    def get(self, university: str) -> UniversityBenefits | None:
        return next((u for u in self.latest() if u.university == university), None)

    def for_olympiad(self, olympiad_id: str) -> list[tuple[UniversityBenefits, list[Rule]]]:
        result = []
        for u in self.latest():
            rules = [r for r in u.rules if r.olympiad.id == olympiad_id]
            if rules:
                result.append((u, rules))
        return result


# ---------------------------------------------------------------------------
# Загрузка и проверка
# ---------------------------------------------------------------------------

def _resolve(catalog: Catalog, query: str) -> CatalogOlympiad | None:
    if query == VSOSH:
        return next((o for o in catalog.olympiads.values() if o.is_vsosh), None)
    found = catalog.find_one(query)
    if found:
        return found
    hits = catalog.search(query, limit=2)
    return hits[0].olympiad if len(hits) == 1 else None


def _list_of(value, kind, where: str, errors: list[str]):
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, kind) for v in value):
        errors.append(f"{where}: ожидался список {kind.__name__} или null, получено {value!r}")
        return None
    return value


def parse_file(data: dict, catalog: Catalog, name: str = "") -> UniversityBenefits:
    errors: list[str] = []

    def need(key: str, kind=str):
        value = data.get(key)
        if not isinstance(value, kind) or (isinstance(value, str) and not value.strip()):
            errors.append(f"{name}: поле «{key}» обязательно ({kind.__name__})")
        return value

    university = need("university")
    year = need("admission_year", int)
    status = need("document_status")
    if status not in DOCUMENT_STATUSES:
        errors.append(f"{name}: document_status — одно из {list(DOCUMENT_STATUSES)}")
    source_url = need("source_url")
    checked = need("checked_at")
    try:
        checked_at = date.fromisoformat(checked) if isinstance(checked, str) else date.min
    except ValueError:
        errors.append(f"{name}: checked_at в формате ГГГГ-ММ-ДД")
        checked_at = date.min
    defaults = data.get("defaults") or {}

    rules: list[Rule] = []
    for i, raw in enumerate(data.get("rules") or [], start=1):
        where = f"{name}, правило {i} ({raw.get('olympiad', '?')})"
        merged = {**defaults, **raw}
        query = merged.get("olympiad")
        olympiad = _resolve(catalog, query) if isinstance(query, str) else None
        if olympiad is None:
            errors.append(f"{where}: олимпиада не найдена в перечне — уточните название")
            continue
        benefit = merged.get("benefit")
        if benefit not in BENEFIT_TYPES:
            errors.append(f"{where}: benefit — одно из {list(BENEFIT_TYPES)}")
        diploma = merged.get("diploma", "any")
        if diploma not in DIPLOMAS:
            errors.append(f"{where}: diploma — одно из {list(DIPLOMAS)}")
        profiles = _list_of(merged.get("profiles"), str, where, errors)
        if profiles:
            known = set(olympiad.profiles)
            unknown = [p for p in profiles if p not in known]
            if unknown:
                errors.append(f"{where}: у олимпиады нет профилей {unknown}; есть: {sorted(known)}")
        levels = _list_of(merged.get("levels"), int, where, errors)
        if levels and not set(levels) <= {1, 2, 3}:
            errors.append(f"{where}: уровни — из 1, 2, 3")
        score = merged.get("confirm_min_score")
        if score is not None and not (isinstance(score, int) and 0 < score <= 100):
            errors.append(f"{where}: confirm_min_score — число от 1 до 100")
        rules.append(Rule(
            olympiad_query=query,
            olympiad=olympiad,
            benefit=benefit,
            diploma=diploma,
            profiles=profiles,
            levels=levels,
            programs=_list_of(merged.get("programs"), str, where, errors),
            confirm_min_score=score,
            confirm_subject=merged.get("confirm_subject"),
            grades=_list_of(merged.get("grades"), int, where, errors),
            diploma_since=merged.get("diploma_since"),
            note=raw.get("note"),
        ))
    if not rules and not errors:
        errors.append(f"{name}: нет ни одного правила")
    if errors:
        raise BenefitsError("\n".join(errors))
    return UniversityBenefits(
        university=university, full_name=data.get("full_name", university), city=data.get("city", ""),
        admission_year=year, document_status=status, source_url=source_url,
        source_title=data.get("source_title", ""), checked_at=checked_at,
        notes=list(data.get("notes") or []), rules=rules, file=name,
    )


def load_benefits(folder: Path, catalog: Catalog) -> BenefitsCatalog:
    """Загружает все файлы. Ошибки во всех файлах собираются в одно исключение."""
    universities, errors = [], []
    for path in sorted(Path(folder).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            universities.append(parse_file(data, catalog, path.name))
        except json.JSONDecodeError as exc:
            errors.append(f"{path.name}: не JSON ({exc})")
        except BenefitsError as exc:
            errors.append(str(exc))
    if errors:
        raise BenefitsError("\n".join(errors))
    return BenefitsCatalog(universities)
