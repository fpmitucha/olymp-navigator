"""Льготы вузов для победителей и призёров олимпиад.

Данные — JSON-файлы в папке benefits/ (один файл = один вуз и один год приёма), их собирают
и проверяют вручную по официальным документам вузов. Формат описан в benefits/README.md.

Правило: олимпиада (+ профили, уровни) → какая льгота (БВИ или 100 баллов), кому (победителям,
призёрам), на какие направления, при каком ЕГЭ. Общие условия вуза задаются в `defaults`
и действуют для всех правил, пока правило не переопределит их.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.catalog import Catalog, CatalogOlympiad

BENEFIT_TYPES = {"bvi": "БВИ", "score100": "100 баллов ЕГЭ", "dvi100": "100 баллов за ДВИ"}
DIPLOMAS = {"winner": "победителям", "prize": "призёрам", "any": "победителям и призёрам"}
DOCUMENT_STATUSES = {"draft": "проект", "approved": "утверждено"}
VSOSH = "ВсОШ"
# Сокращения в таблицах вузов → названия профилей в перечне (ВсОШ)
PROFILE_ALIASES = {
    "искусство (мхк)": "искусство (мировая художественная культура)",
    "мхк": "искусство (мировая художественная культура)",
    "технология": "труд (технология)",
    "обж": "основы безопасности и защиты родины",
}
WILDCARD = "*"   # «любая олимпиада перечня» с нужным профилем / предметом и уровнем
_ROMAN = {1: "I", 2: "II", 3: "III"}


class BenefitsError(ValueError):
    pass


@dataclass
class Rule:
    olympiad_query: str            # как записано в файле — для сообщений об ошибках
    olympiad: CatalogOlympiad
    benefit: str                   # bvi | score100 (ЕГЭ) | dvi100 (доп. вступительное испытание, МГУ)
    diploma: str                   # winner | prize | any
    profiles: list[str] | None     # None — любой профиль (или вуз ещё не уточнил)
    levels: list[int] | None       # None — любой уровень
    programs: list[str] | None     # None — все направления, соответствующие профилю
    confirm_min_score: int | None  # минимальный балл подтверждающего ЕГЭ
    confirm_subject: str | None    # предмет; None — «соответствующий профилю олимпиады»
    grades: list[int] | None       # за какой класс засчитывается диплом
    diploma_since: int | None      # диплом не раньше этого года
    note: str | None
    score_for: str | None = None   # за что ставят 100 баллов: «ЕГЭ по физике», «ДВИ по математике»
    wildcard: str | None = None    # правило «любая олимпиада…» — его описание для экранов вуза


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

# Латинские буквы, которые выглядят как кириллические: в таблицах вузов их иногда набирают по ошибке
_HOMOGLYPHS = str.maketrans("aceopxyABCEHKMOPTX", "асеорхуАВСЕНКМОРТХ")


def _fix_homoglyphs(text: str) -> str:
    """Меняет латинские двойники на кириллицу в словах, где есть кириллица («Bысшая» → «Высшая»)."""
    text = re.sub(r"[\u00ad\u200b-\u200d\u2060\ufeff]", "", text)  # мягкие переносы, невидимые пробелы
    text = re.sub(r"\s+[-–—]\s*|\s*[-–—]\s+", " - ", text)  # «исследователи -будущее» → «исследователи - будущее»
    words = []
    for word in text.split(" "):
        if any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in word):
            word = word.translate(_HOMOGLYPHS)
        words.append(word)
    return " ".join(words)


def _resolve(catalog: Catalog, query: str) -> CatalogOlympiad | None:
    if query == VSOSH:
        return next((o for o in catalog.olympiads.values() if o.is_vsosh), None)
    for text in dict.fromkeys((query, _fix_homoglyphs(query))):
        found = catalog.find_one(text)
        if found:
            return found
        hits = catalog.search(text, limit=2)
        if len(hits) == 1:
            return hits[0].olympiad
    # «Олимпиада Вернадского (Междисциплинарная олимпиада …)» — без пояснения в скобках или по нему
    for part in re.findall(r"^(.*?)\s*\((.+)\)\s*$", query):
        for text in part:
            found = catalog.find_one(text) or next(iter(
                [h.olympiad for h in catalog.search(text, limit=2)] if len(catalog.search(text, limit=2)) == 1 else []
            ), None)
            if found:
                return found
    # «Открытая олимпиада СКФУ «45 параллель»» — по тексту в кавычках
    quoted = re.findall(r"[«“\"]([^«»“”\"]{4,})[»”\"]", _fix_homoglyphs(query))
    if quoted:
        hits = catalog.search(quoted[-1], limit=2)
        if len(hits) == 1:
            return hits[0].olympiad
    return None


def _norm(text: str) -> str:
    return re.sub(r"(\w)- (\w)", r"\1-\2", text).lower().replace("ё", "е").strip()


def wildcard_matches(catalog: Catalog, profiles: list[str] | None, subjects: list[str] | None,
                     levels: list[int] | None, academic_year: str) -> list[tuple[CatalogOlympiad, str]]:
    """Олимпиады перечня года с профилем из `profiles` (или с предметом из `subjects`) нужного уровня.
    Возвращает пары (олимпиада, название профиля в каталоге)."""
    want_profiles = {_norm(p) for p in profiles or []}
    want_subjects = {_norm(x) for x in subjects or []}
    wanted_levels = set(levels or [1, 2, 3])
    found = []
    for o in catalog.olympiads.values():
        if o.is_vsosh:
            continue
        for name, p in o.profiles_in(academic_year).items():
            if p.level not in wanted_levels:
                continue
            if want_profiles and not _profile_fits(name, want_profiles):
                continue
            if want_subjects and not want_subjects & {_norm(x) for x in p.subjects}:
                continue
            if want_profiles or want_subjects:
                found.append((o, name))
    return sorted(found, key=lambda x: (x[0].name, x[1]))


def _profile_fits(catalog_name: str, wanted: set[str]) -> bool:
    """Профиль каталога подходит: совпадает, содержит нужный как часть составного профиля НТО
    («беспилотный транспорт: …, летающая робототехника») или отличается опечаткой."""
    norm = _norm(catalog_name)
    if norm in wanted:
        return True
    parts = {p.strip() for p in re.split(r"[:,]", norm)}
    if parts & wanted or any(len(w) >= 20 and norm.startswith(w) for w in wanted):
        return True
    return any(difflib.SequenceMatcher(None, norm, w).ratio() >= 0.9 for w in wanted)


def wildcard_label(profiles: list[str] | None, subjects: list[str] | None, levels: list[int] | None) -> str:
    """«любая олимпиада перечня I–II уровня по профилю «математика»»."""
    label = "любая олимпиада перечня"
    if levels and set(levels) != {1, 2, 3}:
        lv = sorted(levels)
        label += f" {_ROMAN[lv[0]]}{'–' + _ROMAN[lv[-1]] if len(lv) > 1 else ''} уровня"
    if profiles:
        label += " по профилю «" + "», «".join(profiles) + "»"
    elif subjects:
        label += " с предметом «" + "», «".join(subjects) + "»"
    return label


def resolve_import(rules: list[dict], catalog: Catalog) -> tuple[list[dict], list[str]]:
    """Для импорта таблиц вузов: оставляет правила, чьи олимпиада и профиль нашлись в каталоге.
    Названия профилей приводятся к написанию каталога (регистр, «ё»). Возвращает (правила, проблемы)."""
    kept, problems = [], []
    for rule in rules:
        if rule["olympiad"] == WILDCARD:
            year = rule.get("rsr_year") or catalog.current_year
            if not wildcard_matches(catalog, rule.get("profiles"), rule.get("subjects"), rule.get("levels"), year):
                problems.append(f"«любая олимпиада»: в перечне {year} нет подходящих — "
                                f"{wildcard_label(rule.get('profiles'), rule.get('subjects'), rule.get('levels'))}")
                continue
            kept.append(rule)
            continue
        olympiad = _resolve(catalog, rule["olympiad"])
        if olympiad is None:
            problems.append(f"олимпиада не найдена: «{rule['olympiad']}»")
            continue
        profiles = rule.get("profiles")
        if profiles:
            by_norm = {p.lower().replace("ё", "е"): p for p in olympiad.profiles}

            def match(name: str) -> str | None:
                norm = _fix_homoglyphs(name).lower().replace("ё", "е").strip()
                norm = PROFILE_ALIASES.get(norm, norm)
                if norm in by_norm:
                    return by_norm[norm]
                # обрезанное в таблице название: «…химический инжинирин» → единственный профиль с таким началом
                prefixed = [p for n, p in by_norm.items() if len(norm) >= 10 and n.startswith(norm)]
                if len(prefixed) == 1:
                    return prefixed[0]
                # опечатка: «разработка мобильных предложений» → «…приложений»
                close = difflib.get_close_matches(norm, list(by_norm), n=2, cutoff=0.9)
                if len(close) == 1:
                    return by_norm[close[0]]
                # вместо профиля указан предмет: «информатика» у олимпиады с профилем «искусственный интеллект»
                by_subject = [name for name, by_year in olympiad.profiles.items()
                              if any(norm in {_norm(x) for x in p.subjects} for p in by_year.values())]
                if len(by_subject) == 1:
                    guessed.append(f"в таблице вуза профиль «{name}», в перечне — «{by_subject[0]}»")
                    return by_subject[0]
                return None

            guessed: list[str] = []
            fixed = [match(p) for p in profiles]
            if None in fixed:
                problems.append(f"профиль {profiles} не найден у «{olympiad.name}»; есть: {sorted(olympiad.profiles)}")
                continue
            rule = {**rule, "profiles": fixed}
            if guessed:
                rule["note"] = "; ".join(([rule["note"]] if rule.get("note") else []) + guessed)
        kept.append(rule)
    return kept, sorted(set(problems))


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
        if query == WILDCARD:
            rules.extend(_wildcard_rules(merged, catalog, where, errors))
            continue
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
            note=merged.get("note"),
            score_for=merged.get("score_for"),
        ))
    if not rules and not errors and not data.get("rules"):
        errors.append(f"{name}: нет ни одного правила")
    if errors:
        raise BenefitsError("\n".join(errors))
    return UniversityBenefits(
        university=university, full_name=data.get("full_name", university), city=data.get("city", ""),
        admission_year=year, document_status=status, source_url=source_url,
        source_title=data.get("source_title", ""), checked_at=checked_at,
        notes=list(data.get("notes") or []), rules=rules, file=name,
    )


def _wildcard_rules(merged: dict, catalog: Catalog, where: str, errors: list[str]) -> list[Rule]:
    """«*» раскрывается при загрузке: по правилу на каждую подходящую олимпиаду перечня."""
    profiles = _list_of(merged.get("profiles"), str, where, errors)
    subjects = _list_of(merged.get("subjects"), str, where, errors)
    levels = _list_of(merged.get("levels"), int, where, errors)
    if not profiles and not subjects:
        errors.append(f"{where}: для «*» нужен profiles или subjects")
        return []
    if merged.get("benefit") not in BENEFIT_TYPES or merged.get("diploma", "any") not in DIPLOMAS:
        errors.append(f"{where}: benefit — одно из {list(BENEFIT_TYPES)}, diploma — одно из {list(DIPLOMAS)}")
        return []
    year = merged.get("rsr_year") or catalog.current_year
    label = wildcard_label(profiles, subjects, levels)
    return [Rule(
        olympiad_query=WILDCARD,
        olympiad=o,
        benefit=merged["benefit"],
        diploma=merged.get("diploma", "any"),
        profiles=[profile],
        levels=levels,
        programs=_list_of(merged.get("programs"), str, where, errors),
        confirm_min_score=merged.get("confirm_min_score"),
        confirm_subject=merged.get("confirm_subject"),
        grades=_list_of(merged.get("grades"), int, where, errors),
        diploma_since=merged.get("diploma_since"),
        note=merged.get("note") or label,
        score_for=merged.get("score_for"),
        wildcard=label,
    ) for o, profile in wildcard_matches(catalog, profiles, subjects, levels, year)]


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
