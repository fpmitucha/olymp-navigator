"""Подбор от вуза: «хочу на эту программу — какие олимпиады этого сезона дают льготу».

Берём правила вуза, оставляем те, что действуют на выбранную программу и тип диплома,
и для каждого профиля олимпиады проверяем уровень в перечне текущего учебного года:
диплом, полученный в этом сезоне, будет оцениваться по нему.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.benefits import BENEFIT_TYPES, BenefitsCatalog, Rule, UniversityBenefits
from app.catalog import CatalogOlympiad

CODE = re.compile(r"\d\d\.\d\d\.\d\d")
_ALL = re.compile(r"(?i)^все")


def program_names(benefits: BenefitsCatalog) -> dict[str, str]:
    """Код направления → название («38.03.01» → «Экономика») из документов, где оно написано."""
    names: dict[str, str] = {}
    for u in benefits.latest():
        for r in u.rules:
            for p in r.programs or ():
                m = re.match(r"(\d\d\.\d\d\.\d\d)\s+([^—(]+)", p)
                if m:
                    names.setdefault(m.group(1), m.group(2).strip())
    for code, name in direction_names().items():     # коды, у которых в документах вузов нет названия
        names.setdefault(code, name)
    return names


_CATALOG_FILE = Path(__file__).resolve().parent.parent / "benefits" / "programs" / "catalog.json"


@lru_cache(maxsize=1)
def program_catalog() -> dict[str, list[str]]:
    """Официальные списки программ вузов сверх документов о льготах (benefits/programs/catalog.json)."""
    try:
        data = json.loads(_CATALOG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {uni: list(v.get("programs") or []) for uni, v in data.items() if not uni.startswith("_")}


@lru_cache(maxsize=1)
def direction_names() -> dict[str, str]:
    """Названия направлений по коду из перечня Минобрнауки (catalog.json, «_direction_names»)."""
    try:
        data = json.loads(_CATALOG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {k: v for k, v in (data.get("_direction_names") or {}).items() if CODE.fullmatch(k)}


def university_programs(u: UniversityBenefits) -> list[str]:
    """Программы вуза для выбора цели и подбора.

    Берём программы из правил; коды из «все, кроме …» — это тоже программы вуза;
    плюс официальный список вуза из каталога — только коды, которых в правилах нет
    (иначе одна программа появилась бы дважды под разными названиями).
    """
    found = {p for r in u.rules for p in r.programs or () if not _ALL.match(p)}
    codes = {m.group() for p in found for m in [CODE.match(p)] if m}
    for r in u.rules:
        for p in r.programs or ():
            if _ALL.match(p):
                codes_here = set(CODE.findall(p)) - codes
                found |= codes_here
                codes |= codes_here
    for p in program_catalog().get(u.university, []):
        m = CODE.match(p)
        if m and m.group() not in codes:
            found.add(p)
            codes.add(m.group())
    return sorted(found, key=lambda p: (CODE.match(p) is None, p))


def program_label(program: str, names: dict[str, str]) -> str:
    """Голый код («38.03.01», как у МИФИ) дополняем названием."""
    if CODE.fullmatch(program) and program in names:
        return f"{program} {names[program]}"
    return program


def covers(rule: Rule, program: str) -> bool:
    """Действует ли правило на программу. Пустая программа — «любая»."""
    if not program or not rule.programs:
        return True
    if program in rule.programs:
        return True
    code = CODE.match(program)
    for p in rule.programs:
        if _ALL.match(p) and not (code and code.group() in CODE.findall(p)):
            return True
    return False


@dataclass
class RouteProfile:
    profile: str
    level: int | None         # уровень в перечне текущего года (у ВсОШ — None)
    benefit: str
    min_score: int | None
    subject: str | None
    note: str | None = None


@dataclass
class RouteItem:
    olympiad: CatalogOlympiad
    profiles: list[RouteProfile] = field(default_factory=list)

    @property
    def best(self) -> str:
        return min((p.benefit for p in self.profiles), key=list(BENEFIT_TYPES).index)


def _rank(benefit: str) -> int:
    return list(BENEFIT_TYPES).index(benefit)


def route(u: UniversityBenefits, program: str, diploma: str, academic_year: str) -> list[RouteItem]:
    """Олимпиады сезона academic_year, диплом которых (winner | prize) даёт льготу на программу."""
    best: dict[tuple[str, str], RouteProfile] = {}
    olympiads: dict[str, CatalogOlympiad] = {}
    for r in u.rules:
        if r.diploma not in ("any", diploma) or not covers(r, program):
            continue
        o = r.olympiad
        current = o.profiles_in(academic_year)
        for profile in r.profiles or list(current):
            if profile not in current:
                continue  # профиля нет в перечне этого года — диплом сезона не даст льготы
            level = None if o.is_vsosh else current[profile].level
            if level and r.levels and level not in r.levels:
                continue
            candidate = RouteProfile(profile, level, r.benefit, r.confirm_min_score, r.confirm_subject, r.note)
            key = (o.id, profile)
            old = best.get(key)
            if old is None or (_rank(candidate.benefit), candidate.min_score or 0) < (
                    _rank(old.benefit), old.min_score or 0):
                best[key] = candidate
            olympiads[o.id] = o
    items: dict[str, RouteItem] = {}
    for (oid, _), rp in sorted(best.items(), key=lambda kv: (_rank(kv[1].benefit), kv[0][1])):
        items.setdefault(oid, RouteItem(olympiads[oid])).profiles.append(rp)
    return sorted(items.values(), key=lambda it: (not it.olympiad.is_vsosh, _rank(it.best),
                                                  -sum(p.benefit == it.best for p in it.profiles),
                                                  it.olympiad.name))
