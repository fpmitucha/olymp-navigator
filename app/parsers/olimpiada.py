"""Парсер расписаний с olimpiada.ru (сайт МЦНМО, на его календарь ссылается РСОШ).

Как устроен сайт (проверено 15.10.2026):

* robots.txt запрещает роботам /include (там подгрузка списка) и /activities/*, но разрешает
  страницы олимпиад /activity/<id> и sitemap.xml. Поэтому список берём из sitemap.xml,
  а новые страницы, которых там ещё нет, — со «общих» страниц олимпиад.
* Крупные олимпиады разбиты по предметам: «Физтех» по математике (/activity/395) и по физике
  (/activity/394) — отдельные страницы; общая страница (/activity/123) ссылается на них
  в блоках div.o-info, а у предметной страницы ссылка на общую — в #top_level_activity.
* На странице олимпиады:
  - блок .f_blocks «В 2026/27 году номер 38, уровень 1» — номер в перечне РСОШ;
  - предметы — span.subject_tag в .subject_tags_full;
  - расписание — table.events_for_activity; прошедшие события — строки с классом grey;
    у каждого события постоянный id: /activity/395/events/23149.
* Даты без года: «20 авг...22 сен», «29 сен», «До 31 окт», «Уточняется».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from bs4 import BeautifulSoup

BASE_URL = "https://olimpiada.ru"
SITEMAP_URL = BASE_URL + "/sitemap.xml"
ACTIVITY_URL = BASE_URL + "/activity/{id}"

MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
_DAY_MONTH = r"(\d{1,2})\s+([а-я]{3})[а-я]*\.?"
_RANGE_RE = re.compile(rf"^{_DAY_MONTH}\s*(?:\.\.\.|…|-|–|—)\s*{_DAY_MONTH}$")
_SINGLE_RE = re.compile(rf"^{_DAY_MONTH}$")
_DEADLINE_RE = re.compile(rf"^до\s+{_DAY_MONTH}$")
_SAME_MONTH_RANGE_RE = re.compile(r"^(\d{1,2})\s*(?:\.\.\.|…|-|–|—)\s*(\d{1,2})\s+([а-я]{3})[а-я]*\.?$")
_RSR_RE = re.compile(r"В\s+(\d{4})/(\d{2})\s+году\s+номер\s+(\d+)(?:,\s*(.+))?", re.IGNORECASE)
_EVENT_ID_RE = re.compile(r"/activity/(\d+)/events/(\d+)")
_ACTIVITY_HREF_RE = re.compile(r"^(?:https?://olimpiada\.ru)?/activity/(\d+)/?$")
_SITEMAP_ACTIVITY_RE = re.compile(r"<loc>\s*https?://olimpiada\.ru/activity/(\d+)/?\s*</loc>")


@dataclass
class Event:
    event_id: int
    name: str
    kind: str
    when_raw: str
    starts_at: date | None
    ends_at: date | None
    past: bool


@dataclass
class Activity:
    id: int
    name: str
    parent_name: str | None
    subjects: list[str]
    rsr_year: str | None        # «2026/27»
    rsr_number: int | None      # номер в перечне РСОШ
    rsr_levels: str | None      # «уровень 1», «уровни II и III по 23 направлениям»
    events: list[Event] = field(default_factory=list)
    parent_id: int | None = None                         # общая страница олимпиады
    child_ids: list[int] = field(default_factory=list)   # предметные страницы (на общей странице)

    @property
    def url(self) -> str:
        return ACTIVITY_URL.format(id=self.id)

    def event_url(self, event: Event) -> str:
        return f"{self.url}/events/{event.event_id}"


# ---------------------------------------------------------------------------
# Список олимпиад
# ---------------------------------------------------------------------------

def parse_sitemap(xml: str) -> list[int]:
    """Id страниц олимпиад из sitemap.xml, без повторов, в порядке появления."""
    seen: dict[int, None] = {}
    for m in _SITEMAP_ACTIVITY_RE.finditer(xml):
        seen.setdefault(int(m.group(1)), None)
    return list(seen)


# ---------------------------------------------------------------------------
# Страница олимпиады
# ---------------------------------------------------------------------------

def classify_event(name: str) -> str:
    """Вид события по названию: registration, final, results, qualifying, other."""
    text = name.lower()
    if "регистрац" in text:
        return "registration"
    if any(w in text for w in ("результат", "итоги", "апелляц", "разбор")):
        return "results"
    if any(w in text for w in ("заключительн", "финал")):
        return "final"
    if any(w in text for w in ("отбор", "тур", "этап", "олимпиада", "день")):
        return "qualifying"
    return "other"


def _year_for(month: int, academic_start: int) -> int:
    """Учебный год 2026/27: август–декабрь — 2026, январь–июль — 2027."""
    return academic_start if month >= 8 else academic_start + 1


def _date(day: str, month_word: str, academic_start: int) -> date | None:
    month = MONTHS.get(month_word[:3])
    if month is None:
        return None
    try:
        return date(_year_for(month, academic_start), month, int(day))
    except ValueError:
        return None


def parse_when(text: str, academic_start: int) -> tuple[date | None, date | None]:
    """«20 авг...22 сен» → (20.08, 22.09); «До 31 окт» → (None, 31.10); «Уточняется» → (None, None)."""
    t = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip().lower()
    if m := _RANGE_RE.match(t):
        start = _date(m.group(1), m.group(2), academic_start)
        end = _date(m.group(3), m.group(4), academic_start)
        if start and end and end < start:  # «15 июл...20 авг»: лето на стыке учебных лет
            start = start.replace(year=end.year if start.month <= end.month else end.year - 1)
        return start, end
    if m := _SAME_MONTH_RANGE_RE.match(t):
        return _date(m.group(1), m.group(3), academic_start), _date(m.group(2), m.group(3), academic_start)
    if m := _DEADLINE_RE.match(t):
        return None, _date(m.group(1), m.group(2), academic_start)
    if m := _SINGLE_RE.match(t):
        d = _date(m.group(1), m.group(2), academic_start)
        return d, d
    return None, None


def academic_start_for(today: date) -> int:
    """Первый год текущего учебного года: с августа — этот год, иначе прошлый."""
    return today.year if today.month >= 8 else today.year - 1


def parse_activity(html: str, activity_id: int, today: date) -> Activity:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    parent = soup.select_one("#top_level_activity a")
    subjects = [_text(tag).replace("\xa0", " ").strip() for tag in soup.select(".subject_tags_full .subject_tag")]

    rsr_year = rsr_number = rsr_levels = None
    for block in soup.select(".f_blocks"):
        text = _text(block)
        if "Перечне" not in text:
            continue
        if m := _RSR_RE.search(text):
            rsr_year = f"{m.group(1)}/{m.group(2)}"
            rsr_number = int(m.group(3))
            levels = (m.group(4) or "").split("Перечень")[0].strip(" ,")
            rsr_levels = levels or None
        break

    academic_start = int(rsr_year[:4]) if rsr_year else academic_start_for(today)
    events: list[Event] = []
    table = soup.select_one("table.events_for_activity")
    for row in table.select("tbody tr") if table else []:
        link = row.find("a", href=_EVENT_ID_RE)
        name_tag = row.select_one(".event_name")
        if not link or not name_tag:
            continue  # строка «Прошедшие события» и подобные
        cells = row.find_all("td")
        when_raw = _text(cells[-1]) if cells else ""
        start, end = parse_when(when_raw, academic_start)
        name = _text(name_tag)
        events.append(Event(
            event_id=int(_EVENT_ID_RE.search(link["href"]).group(2)),
            name=name,
            kind=classify_event(name),
            when_raw=when_raw,
            starts_at=start,
            ends_at=end,
            past="grey" in (row.get("class") or []),
        ))

    parent_id = None
    if parent and (m := _ACTIVITY_HREF_RE.match(parent.get("href", ""))):
        parent_id = int(m.group(1))
    child_ids: list[int] = []
    for link in soup.select(".o-info a[href]"):
        if (m := _ACTIVITY_HREF_RE.match(link["href"])) and int(m.group(1)) not in child_ids \
                and int(m.group(1)) != activity_id:
            child_ids.append(int(m.group(1)))

    return Activity(
        id=activity_id,
        parent_id=parent_id,
        child_ids=child_ids,
        name=_text(h1) if h1 else "",
        parent_name=_text(parent) if parent else None,
        subjects=subjects,
        rsr_year=rsr_year,
        rsr_number=rsr_number,
        rsr_levels=rsr_levels,
        events=events,
    )


def _text(tag) -> str:
    return re.sub(r"\s+", " ", tag.get_text(" ").replace("\xa0", " ")).strip()
