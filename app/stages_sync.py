"""Синхронизация дат этапов с olimpiada.ru в базу бота.

Обход соблюдает robots.txt сайта: только sitemap.xml и страницы олимпиад /activity/<id>.

1. Поиск страниц (раз в месяц или когда список пуст): все олимпиады из sitemap.xml;
   нужные — те, где написано «В 2026/27 году номер N» (олимпиада из перечня РСОШ).
   Попутно запоминаем их общие страницы («Физтех» → «Физтех» по математике, по физике…).
2. Ежедневно: общие страницы — чтобы найти новые предметные, которых ещё нет в sitemap,
   и все известные страницы перечня — их расписание.

Страница сопоставляется с олимпиадой каталога по номеру в перечне, а при неудаче — по
названию. Профиль определяется по предмету страницы. Сохраняются будущие и идущие
события с известной датой; пропавшие с сайта удаляются.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from app.bot.storage import Storage
from app.catalog import Catalog, CatalogOlympiad, normalize
from app.parsers.olimpiada import ACTIVITY_URL, SITEMAP_URL, Activity, parse_activity, parse_sitemap
from app.stages import StageRow

log = logging.getLogger(__name__)

EXTERNAL_PREFIX = "olimpiada"
DISCOVERY_EVERY = timedelta(days=30)
META_RELEVANT = "olimpiada_relevant_ids"
META_PARENTS = "olimpiada_parent_ids"
META_DISCOVERED = "olimpiada_discovered_at"

Fetch = Callable[[str], str]
Progress = Callable[[int, int, str], None]


@dataclass
class SyncReport:
    discovery: bool = False
    pages_checked: int = 0
    activities: int = 0
    matched: int = 0
    stages_saved: int = 0
    stages_removed: int = 0
    without_schedule: int = 0
    unmatched: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = f"поиск страниц: проверено {self.pages_checked}; " if self.discovery else ""
        return (f"{head}олимпиад перечня на сайте: {self.activities}, сопоставлено: {self.matched}, "
                f"этапов сохранено: {self.stages_saved}, удалено устаревших: {self.stages_removed}, "
                f"без расписания: {self.without_schedule}, не сопоставлено: {len(self.unmatched)}, "
                f"ошибок: {len(self.errors)}")


# ---------------------------------------------------------------------------
# Сопоставление с каталогом
# ---------------------------------------------------------------------------

def _stems(text: str) -> list[str]:
    """Основы слов для сравнения разных падежей: «математика» и «по математике» → «математи»."""
    words = [w for w in re.split(r"[^а-яa-z]+", normalize(text)) if len(w) >= 4]
    return [w[:-2] if len(w) > 5 else w for w in words]


def match_profile(o: CatalogOlympiad, activity: Activity, academic_year: str) -> str | None:
    """Профиль олимпиады, к которому относится страница, или None — если ко всей олимпиаде."""
    profiles = list(o.profiles_in(academic_year)) or list(o.profiles)
    if len(profiles) <= 1:
        return None
    name = normalize(activity.name)
    specific = name.split(" по ", 1)[1] if " по " in name else ""
    candidates = []
    if specific:
        for profile in profiles:
            stems = _stems(profile)
            if stems and all(stem in specific for stem in stems):
                candidates.append(profile)
    if not candidates:
        tags = {normalize(s) for s in activity.subjects}
        candidates = [p for p in profiles if normalize(p) in tags]
    if not candidates:
        return None
    return max(candidates, key=len)  # «информатика и программирование» точнее, чем «информатика»


def match_olympiad(catalog: Catalog, activity: Activity) -> CatalogOlympiad | None:
    if activity.rsr_number is not None:
        found = catalog.by_number(activity.rsr_year or catalog.current_year, activity.rsr_number)
        if found:
            return found
    for name in filter(None, (activity.parent_name, activity.name)):
        found = catalog.find_one(name)
        if found:
            return found
        hits = catalog.search(name, limit=2)
        if len(hits) == 1:
            return hits[0].olympiad
    return None


def stage_rows(activity: Activity, olympiad: CatalogOlympiad, profile: str | None, academic_year: str,
               today: date) -> list[StageRow]:
    """Будущие и идущие события страницы с известной датой."""
    label = profile
    is_subject_page = " по " in normalize(activity.name)  # «Высшая проба по истории», а не вся НТО
    if label is None and is_subject_page and len(olympiad.profiles_in(academic_year)) > 1 and activity.subjects:
        # Страница одного предмета многопрофильной олимпиады, профиль не угадан — подписываем предметами
        label = ", ".join(s.lower() for s in activity.subjects[:3])
    rows = []
    for e in activity.events:
        last = e.ends_at or e.starts_at
        if e.past or last is None or last < today:
            continue
        rows.append(StageRow(
            olympiad_id=olympiad.id,
            kind=e.kind,
            title=e.name,
            starts_at=e.starts_at,
            ends_at=e.ends_at,
            profile=label,
            academic_year=academic_year,
            source_url=activity.event_url(e),
            external_id=f"{EXTERNAL_PREFIX}:{activity.id}:{e.event_id}",
        ))
    return rows


# ---------------------------------------------------------------------------
# Обход сайта
# ---------------------------------------------------------------------------

class _Crawler:
    def __init__(self, fetch: Fetch, today: date, report: SyncReport):
        self.fetch, self.today, self.report = fetch, today, report
        self.cache: dict[int, Activity | None] = {}

    def get(self, activity_id: int) -> Activity | None:
        if activity_id not in self.cache:
            self.report.pages_checked += 1
            try:
                self.cache[activity_id] = parse_activity(
                    self.fetch(ACTIVITY_URL.format(id=activity_id)), activity_id, self.today)
            except Exception as exc:  # noqa: BLE001 — одна сломанная страница не останавливает обход
                self.report.errors.append(f"activity/{activity_id}: {exc}")
                log.warning("Не удалось загрузить olimpiada.ru/activity/%s: %s", activity_id, exc)
                self.cache[activity_id] = None
        return self.cache[activity_id]


def _is_relevant(a: Activity | None, academic_year: str) -> bool:
    return a is not None and a.rsr_number is not None and a.rsr_year == academic_year


def _collect(crawler: _Crawler, candidates: list[int], parents: set[int], academic_year: str,
             progress: Progress | None, stage: str) -> set[int]:
    """Проверяет страницы и их общие страницы; возвращает id страниц перечня."""
    relevant: set[int] = set()
    queue = list(dict.fromkeys(candidates))
    done: set[int] = set()
    # общие страницы — первыми: с них находятся предметные страницы, которых нет в sitemap
    queue = [p for p in parents if p not in queue] + queue
    while queue:
        activity_id = queue.pop(0)
        if activity_id in done:
            continue
        done.add(activity_id)
        if progress:
            progress(len(done), len(done) + len(queue), f"{stage}: activity/{activity_id}")
        a = crawler.get(activity_id)
        if a is None:
            continue
        if _is_relevant(a, academic_year):
            relevant.add(activity_id)
            if a.parent_id and a.parent_id not in parents:
                parents.add(a.parent_id)
                queue.append(a.parent_id)
        if activity_id in parents:
            queue.extend(c for c in a.child_ids if c not in done)
    return relevant


def sync_stages(
    catalog: Catalog,
    storage: Storage,
    fetch: Fetch,
    today: date,
    *,
    full: bool = False,
    limit: int | None = None,
    progress: Progress | None = None,
) -> SyncReport:
    """Обновляет даты этапов. full=True — заново найти все страницы перечня через sitemap.xml."""
    report = SyncReport()
    academic_year = catalog.current_year
    crawler = _Crawler(fetch, today, report)

    known = set(json.loads(storage.get_meta(META_RELEVANT) or "[]"))
    parents = set(json.loads(storage.get_meta(META_PARENTS) or "[]"))
    discovered_at = storage.get_meta(META_DISCOVERED)
    stale = not discovered_at or datetime.now(timezone.utc) - datetime.fromisoformat(discovered_at) > DISCOVERY_EVERY
    report.discovery = full or not known or stale

    if report.discovery:
        candidates = parse_sitemap(fetch(SITEMAP_URL))
        if limit:
            candidates = candidates[:limit]
        relevant = _collect(crawler, candidates, parents, academic_year, progress, "поиск страниц")
    else:
        relevant = _collect(crawler, sorted(known), parents, academic_year, progress, "обновление")
    if limit:
        relevant = set(sorted(relevant)[:limit])
    # Страница не загрузилась (таймаут, сбой сайта) — не считаем, что она выбыла из перечня
    failed = {aid for aid, a in crawler.cache.items() if a is None}
    keep = relevant | (known & failed)

    if not limit:  # пробный прогон с --limit не портит сохранённый список
        storage.set_meta(META_RELEVANT, json.dumps(sorted(keep)))
        storage.set_meta(META_PARENTS, json.dumps(sorted(parents)))
        if report.discovery:
            storage.set_meta(META_DISCOVERED, datetime.now(timezone.utc).isoformat(timespec="seconds"))

    for activity_id in sorted(relevant):
        activity = crawler.get(activity_id)
        report.activities += 1
        olympiad = match_olympiad(catalog, activity)
        if olympiad is None:
            report.unmatched.append(f"{activity_id} {activity.name}")
            continue
        report.matched += 1
        profile = match_profile(olympiad, activity, academic_year)
        rows = stage_rows(activity, olympiad, profile, academic_year, today)
        if not rows:
            report.without_schedule += 1
        saved, removed = storage.replace_external_stages(f"{EXTERNAL_PREFIX}:{activity_id}:", rows)
        report.stages_saved += saved
        report.stages_removed += removed

    # Страницы, выбывшие из перечня: их этапы больше не нужны
    if not limit:
        for activity_id in known - keep:
            _, removed = storage.replace_external_stages(f"{EXTERNAL_PREFIX}:{activity_id}:", [])
            report.stages_removed += removed
    return report
