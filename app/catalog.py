"""Каталог олимпиад для бота и сайта: перечни всех загруженных лет в памяти.

Данные небольшие (несколько сотен профилей за год), поэтому держим их в памяти
и ищем без базы. Источник — JSON-файлы, которые пишет `python -m app.cli rsr fetch`,
плюс список предметов ВсОШ.

Олимпиада узнаётся между годами по ключу (`olympiad_key`). Для ссылок в кнопках
используется короткий стабильный id — первые символы хеша ключа.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.exporters import load_json
from app.parsers.rsr import Profile, RsrList

# Сокращения, которыми ищут олимпиады. Значение — подстрока ключа олимпиады.
ALIASES: dict[str, str] = {
    "всош": "всероссийская олимпиада школьников",
    "всерос": "всероссийская олимпиада школьников",
    "вшэ": "высшая проба",
    "вышка": "высшая проба",
    "мгу": "олимпиада школьников ломоносов",
    "спбгу": "санкт-петербургского государственного университета",
    "мфти": "физтех",
    "мош": "московская олимпиада школьников",
    "нто": "национальная технологическая олимпиада",
    "пвг": "покори воробьевы горы",
    "оммо": "объединенная межвузовская",
    "мифи": "росатом",
    "бауманка": "шаг в будущее",
    "мгту": "шаг в будущее",
    "итмо": "открытая олимпиада школьников",  # точный ключ: олимпиада ИТМО
    "мгимо": "мгимо",
    "ранхигс": "российской академии народного хозяйства",
}


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[«»\"“”„()]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def short_id(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def year_start(academic_year: str) -> int:
    return int(academic_year[:4])


@dataclass
class YearProfile:
    academic_year: str
    profile: Profile


@dataclass
class CatalogOlympiad:
    id: str
    key: str
    name: str                     # название из самого свежего перечня
    is_vsosh: bool = False
    # профиль -> {учебный год -> Profile}
    profiles: dict[str, dict[str, Profile]] = field(default_factory=dict)
    websites: list[str] = field(default_factory=list)
    numbers: dict[str, int] = field(default_factory=dict)  # учебный год -> номер в перечне
    _haystack: str = ""

    @property
    def years(self) -> list[str]:
        found = {year for by_year in self.profiles.values() for year in by_year}
        return sorted(found, key=year_start, reverse=True)

    @property
    def latest_year(self) -> str:
        return self.years[0]

    def profiles_in(self, academic_year: str) -> dict[str, Profile]:
        return {name: by_year[academic_year] for name, by_year in self.profiles.items() if academic_year in by_year}

    def best_level(self, academic_year: str, profile_names: set[str] | None = None) -> int | None:
        levels = [
            p.level for name, p in self.profiles_in(academic_year).items()
            if p.level and (profile_names is None or name in profile_names)
        ]
        return min(levels) if levels else None



# ---------------------------------------------------------------------------
# Склейка переименованных олимпиад
# ---------------------------------------------------------------------------

# Переименования, которые не ловятся автоматически (проверено по сайтам олимпиад).
# Пары ключей olympiad_key(название): (старое, новое).
MANUAL_RENAMES: list[tuple[str, str]] = [
    ("международная олимпиада innopolis open", "международная олимпиада иннополиса"),
    ("объединенная межвузовская математическая олимпиада школьников", "объединенная межвузовская олимпиада школьников"),
]

_FILLER_WORDS = {"школьников", "для", "старших", "классов", "класса"}


def _fuzzy_name(name: str) -> str:
    """Название без пунктуации, скобок и слов-наполнителей: «…олимпиада школьников» = «…олимпиада»."""
    text = normalize(re.sub(r"\([^)]*\)", " ", name))
    text = re.sub(r"^[ivxlcdm]+ ", "", text)
    words = [w for w in re.split(r"[^0-9a-zа-я]+", text) if w and w not in _FILLER_WORDS]
    words = [w for w in words if not re.fullmatch(r"\d+(-\d+)?", w)]  # «8–11 классов»
    return " ".join(words)


def _quoted_name(name: str) -> str | None:
    quoted = re.findall(r"«([^«»]+)»", name)
    return normalize(quoted[-1]) if quoted else None


def _norm_url(url: str) -> str:
    url = url.strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    return url.rstrip("/")


def dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for url in urls:
        norm = _norm_url(url)
        if norm and norm not in seen:
            seen.add(norm)
            result.append(url.strip())
    return result


def merge_renamed(records: list[CatalogOlympiad]) -> list[list[CatalogOlympiad]]:
    """Группирует записи одной олимпиады под разными названиями в разные годы.

    Записи склеиваются, если они из РАЗНЫХ лет (годы не пересекаются) и совпадает хотя бы одно:
    название без пунктуации и слов-наполнителей, текст в последних «кавычках», адрес сайта,
    либо пара есть в MANUAL_RENAMES. Условие о непересечении лет защищает от склейки
    разных олимпиад с похожими названиями, которые были в перечне одновременно
    (например, две «Архитектуры и искусства» в 2025/26).
    """
    parent = list(range(len(records)))
    years = [set(r.years) for r in records]

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a == b or years[a] & years[b]:
            return
        if records[i].is_vsosh != records[j].is_vsosh:
            return
        parent[b] = a
        years[a] |= years[b]

    index_by_key = {r.key: i for i, r in enumerate(records)}
    for old, new in MANUAL_RENAMES:
        if old in index_by_key and new in index_by_key:
            union(index_by_key[old], index_by_key[new])

    signals: dict[tuple[str, str], list[int]] = {}
    for i, r in enumerate(records):
        keys = {("fuzzy", _fuzzy_name(r.name))}
        quoted = _quoted_name(r.name)
        if quoted and len(quoted) >= 6:
            keys.add(("quoted", quoted))
        keys |= {("url", _norm_url(u)) for u in r.websites if u}
        for k in keys:
            signals.setdefault(k, []).append(i)
    for members in signals.values():
        for j in members[1:]:
            union(members[0], j)

    groups: dict[int, list[CatalogOlympiad]] = {}
    for i, r in enumerate(records):
        groups.setdefault(find(i), []).append(r)
    return list(groups.values())


def _combine(group: list[CatalogOlympiad]) -> CatalogOlympiad:
    """Объединяет записи одной олимпиады. Главная — с самым свежим годом."""
    ordered = sorted(group, key=lambda r: year_start(r.latest_year), reverse=True)
    main = ordered[0]
    if len(group) == 1:
        return main
    merged = CatalogOlympiad(id=main.id, key=main.key, name=main.name, is_vsosh=main.is_vsosh)
    for r in ordered:
        for profile, by_year in r.profiles.items():
            target = merged.profiles.setdefault(profile, {})
            for year, p in by_year.items():
                target.setdefault(year, p)
        merged.websites.extend(r.websites)
        for year, number in r.numbers.items():
            merged.numbers.setdefault(year, number)
    merged.websites = dedupe_urls(merged.websites)
    return merged


@dataclass
class SearchHit:
    olympiad: CatalogOlympiad
    matched_profiles: list[str]   # профили, совпавшие с запросом (пусто, если совпало название)
    name_match: bool


class Catalog:
    def __init__(self, rsr_lists: list[RsrList], vsosh_lists: list[RsrList] | None = None):
        vsosh_lists = vsosh_lists or []
        vsosh_keys = {o.key for v in vsosh_lists for o in v.olympiads}
        lists = rsr_lists + vsosh_lists

        # 1. Записи по точному ключу названия
        records: dict[str, CatalogOlympiad] = {}
        for rsr in sorted(lists, key=lambda r: year_start(r.academic_year)):
            for o in rsr.olympiads:
                item = records.get(o.key)
                if item is None:
                    item = CatalogOlympiad(id=short_id(o.key), key=o.key, name=o.name, is_vsosh=o.key in vsosh_keys)
                    records[o.key] = item
                item.name = o.name  # списки идут от старых к новым: остаётся самое свежее название
                if o.number is not None:
                    item.numbers[rsr.academic_year] = o.number
                for p in o.profiles:
                    item.profiles.setdefault(p.profile, {})[rsr.academic_year] = p
                    if p.website:
                        item.websites.insert(0, p.website)  # списки идут от старых к новым: свежие сайты в начало

        # 2. Склейка переименованных олимпиад разных лет
        self.olympiads: dict[str, CatalogOlympiad] = {}
        self._by_key: dict[str, CatalogOlympiad] = {}
        self._by_alias_id: dict[str, CatalogOlympiad] = {}
        for group in merge_renamed(list(records.values())):
            merged = _combine(group)
            merged.websites = dedupe_urls(merged.websites)
            self.olympiads[merged.id] = merged
            for member in group:
                self._by_key[member.key] = merged
                self._by_alias_id[member.id] = merged

        # Годы перечня РСОШ; ВсОШ не в счёт (её список есть только на текущий год)
        rsr_years = {r.academic_year for r in rsr_lists} or {r.academic_year for r in vsosh_lists}
        self.years = sorted(rsr_years, key=year_start, reverse=True)
        for item in self.olympiads.values():
            parts = [item.name, item.key]
            for name, by_year in item.profiles.items():
                parts.append(name)
                for p in by_year.values():
                    parts.extend(p.subjects)
            item._haystack = normalize(" | ".join(parts))

    # -- загрузка ----------------------------------------------------------

    @classmethod
    def load(cls, rsr_dir: Path, *, with_vsosh: bool = True) -> "Catalog":
        lists = [load_json(path) for path in sorted(Path(rsr_dir).glob("rsr_*.json"))]
        if not lists:
            raise FileNotFoundError(
                f"В {rsr_dir} нет файлов rsr_*.json. Сначала скачайте перечень: python -m app.cli rsr fetch"
            )
        vsosh: list[RsrList] = []
        if with_vsosh:
            from app.parsers.vsosh import VSOSH_SUBJECTS, vsosh_list

            vsosh = [vsosh_list(year) for year in VSOSH_SUBJECTS]
        return cls(lists, vsosh)

    # -- доступ ------------------------------------------------------------

    @property
    def current_year(self) -> str:
        """Последний учебный год, по которому загружен перечень РСОШ (ВсОШ не учитывается)."""
        return self.years[0]

    def get(self, olympiad_id: str) -> CatalogOlympiad | None:
        """По id. Старые id переименованных олимпиад тоже работают (подписки не теряются)."""
        return self.olympiads.get(olympiad_id) or self._by_alias_id.get(olympiad_id)

    def by_number(self, academic_year: str, number: int) -> CatalogOlympiad | None:
        """Олимпиада по номеру в перечне РСОШ за учебный год."""
        for o in self.olympiads.values():
            if o.numbers.get(academic_year) == number:
                return o
        return None

    def canonical_id(self, olympiad_id: str) -> str:
        found = self.get(olympiad_id)
        return found.id if found else olympiad_id

    def by_key(self, key: str) -> CatalogOlympiad | None:
        return self._by_key.get(key)

    def find_one(self, text: str) -> CatalogOlympiad | None:
        """Олимпиада по точному названию, ключу или id (для импорта этапов)."""
        if self.get(text):
            return self.get(text)
        from app.parsers.rsr import olympiad_key

        return self._by_key.get(olympiad_key(text))

    # -- поиск -------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        """Ищет олимпиады по названию, сокращению, профилю или предмету.

        Все слова запроса должны встретиться у олимпиады. Сначала идут совпадения
        по названию, затем олимпиады, где совпавший профиль имеет более высокий
        уровень (1 выше 3) в текущем перечне.
        """
        q = normalize(query)
        if not q:
            return []
        alias = ALIASES.get(q)
        if alias:
            exact = self._by_key.get(alias)
            if exact:
                return [SearchHit(exact, [], True)]
            hits = [
                SearchHit(o, [], True) for o in self.olympiads.values() if alias in normalize(o.key)
            ]
            return sorted(hits, key=lambda h: (not h.olympiad.is_vsosh, h.olympiad.name))[:limit]

        words = []
        for w in q.split(" "):
            words.extend(ALIASES[w].split(" ") if w in ALIASES else [w])
        hits: list[SearchHit] = []
        for o in self.olympiads.values():
            if not all(w in o._haystack for w in words):
                continue
            name_text = normalize(f"{o.name} {o.key}")
            if all(w in name_text for w in words):
                hits.append(SearchHit(o, [], True))
                continue
            # «ломоносов математика»: часть слов из названия, остальные — из профиля или его предметов
            matched = []
            for name, by_year in o.profiles.items():
                profile_text = normalize(" | ".join([name] + [s for p in by_year.values() for s in p.subjects]))
                if all(w in name_text or w in profile_text for w in words) and any(w in profile_text for w in words):
                    matched.append(name)
            if matched:
                hits.append(SearchHit(o, matched, False))

        current = self.current_year

        def rank(hit: SearchHit):
            o = hit.olympiad
            in_current = current in o.years
            level = o.best_level(current, set(hit.matched_profiles) or None) if in_current else None
            return (
                not hit.name_match,
                not o.is_vsosh,          # ВсОШ даёт БВИ по закону — показываем первой
                not in_current,          # олимпиады текущего перечня выше выбывших
                level or 4,
                o.name,
            )

        hits.sort(key=rank)
        return hits[:limit]
