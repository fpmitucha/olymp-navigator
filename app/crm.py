"""Сопровождение (CRM): статусы олимпиад клиента, его роадмап и тексты уведомлений.

Куратор включает сопровождение по короткому ID из кабинета, ставит цели (вуз + программа) и олимпиады.
Клиент видит роадмап: по каждой олимпиаде четыре шага (регистрация → отбор → финал → итог)
и какие льготы она даёт в его вузах. Смена статуса уходит клиенту в Telegram через очередь outbox.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from html import escape

from app.benefits import BENEFIT_TYPES, BenefitsCatalog
from app.catalog import Catalog, CatalogOlympiad
from app.routes import route

# статус → (подпись, состояния четырёх шагов: d — сделано, n — в работе, x — впереди, f — не пройдено, s — пропуск)
STATUSES: dict[str, tuple[str, str]] = {
    "todo": ("Не начато", "xxxx"),
    "registration": ("Нужна регистрация", "nxxx"),
    "registered": ("Регистрация пройдена", "dxxx"),
    "qualifying": ("Идёт отбор", "dnxx"),
    "passed": ("Отбор пройден", "ddxx"),
    "failed": ("Отбор не пройден", "dfss"),
    "final": ("Идёт финал", "ddnx"),
    "winner": ("Победитель", "dddd"),
    "prize": ("Призёр", "dddd"),
    "participant": ("Участник финала", "dddd"),
}
STEP_NAMES = ("Регистрация", "Отборочный", "Финал", "Итог")
DEADLINE_STATUSES = {"todo", "registration"}   # пока не зарегистрированы — следим за концом регистрации
BENEFIT_SHORT = {"bvi": "БВИ", "score100": "100 баллов", "dvi100": "100 за ДВИ"}


@dataclass
class Step:
    name: str
    state: str   # d | n | x | f | s


@dataclass
class RoadmapItem:
    row_id: int
    olympiad: CatalogOlympiad
    profile: str
    status: str
    deadline: date | None          # конец регистрации: вручную или с сайта олимпиады
    deadline_manual: bool
    benefits: list[tuple[str, str, str]] = field(default_factory=list)  # (вуз, льгота, кому: prize | winner)

    @property
    def label(self) -> str:
        return STATUSES.get(self.status, STATUSES["todo"])[0]

    @property
    def steps(self) -> list[Step]:
        states = STATUSES.get(self.status, STATUSES["todo"])[1]
        return [Step(name, s) for name, s in zip(STEP_NAMES, states)]

    @property
    def tone(self) -> str:
        """Цвет плашки статуса: alert — от клиента что-то нужно, gold — успех, plain — остальное."""
        if self.status == "registration":
            return "alert"
        if self.status in ("passed", "winner", "prize"):
            return "gold"
        return "plain"


@dataclass
class Goal:
    id: int
    university: str
    program: str
    bvi: int = 0      # сколько олимпиад плана дают БВИ (призёру или победителю)
    other: int = 0    # сколько дают 100 баллов


@dataclass
class Roadmap:
    goals: list[Goal]
    items: list[RoadmapItem]

    def count(self, state: str) -> int:
        return sum(s.state == state for it in self.items for s in it.steps)

    @property
    def done(self) -> int:
        return self.count("d")

    @property
    def now(self) -> int:
        return self.count("n")

    @property
    def ahead(self) -> int:
        return self.count("x")

    @property
    def total(self) -> int:
        return self.done + self.now + self.ahead


def _rank(benefit: str) -> int:
    return list(BENEFIT_TYPES).index(benefit)


def goal_benefits(benefits: BenefitsCatalog, goals: list[dict], year: str) -> dict[tuple[str, str], list]:
    """(олимпиада, профиль) → [(вуз, льгота, prize|winner)] — лучшая льгота по каждой цели."""
    out: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
    for g in goals:
        u = benefits.get(g["university"])
        if u is None:
            continue
        for diploma in ("prize", "winner"):
            for item in route(u, g["program"], diploma, year):
                for p in item.profiles:
                    per_uni = out.setdefault((item.olympiad.id, p.profile), {})
                    old = per_uni.get(g["university"])
                    if old is None or _rank(p.benefit) < _rank(old[0]):
                        per_uni[g["university"]] = (p.benefit, diploma)
    return {key: [(uni, b, d) for uni, (b, d) in per_uni.items()] for key, per_uni in out.items()}


def registration_deadline(stages, olympiad_id: str, profile: str, today: date) -> date | None:
    """Ближайший конец регистрации из этапов с сайта олимпиады (этап профиля или общий)."""
    found = [s.ends_at or s.starts_at for s in stages
             if s.kind == "registration" and (not s.profile or s.profile == profile) and s.is_upcoming(today)]
    found = [d for d in found if d]
    return min(found) if found else None


def build_roadmap(catalog: Catalog, benefits: BenefitsCatalog, goals: list[dict], rows: list[dict],
                  stages_for, today: date) -> Roadmap:
    """stages_for(olympiad_id) → этапы олимпиады из базы."""
    year = catalog.current_year
    by_pair = goal_benefits(benefits, goals, year)
    items = []
    for r in rows:
        o = catalog.get(r["olympiad_id"])
        if o is None:
            continue
        auto = registration_deadline(stages_for(o.id), o.id, r["profile"], today)
        items.append(RoadmapItem(
            row_id=r["id"], olympiad=o, profile=r["profile"], status=r["status"],
            deadline=r["reg_deadline"] or auto, deadline_manual=r["reg_deadline"] is not None,
            benefits=sorted(by_pair.get((o.id, r["profile"]), []), key=lambda x: (_rank(x[1]), x[0]))))
    goal_list = []
    for g in goals:
        found = [b for it in items for (uni, b, _) in it.benefits if uni == g["university"]]
        goal_list.append(Goal(g["id"], g["university"], g["program"], bvi=found.count("bvi"),
                              other=len(found) - found.count("bvi")))
    return Roadmap(goal_list, items)


def suggestions(catalog: Catalog, benefits: BenefitsCatalog, goals: list[dict], taken: set[tuple[str, str]],
                limit: int = 8) -> list[tuple[CatalogOlympiad, str, list[str]]]:
    """Олимпиады, которые дают БВИ призёру в наибольшем числе вузов-целей и ещё не в плане."""
    by_pair = goal_benefits(benefits, goals, catalog.current_year)
    ranked = []
    for (oid, profile), found in by_pair.items():
        if (oid, profile) in taken:
            continue
        unis = sorted(uni for uni, b, d in found if b == "bvi" and d == "prize")
        o = catalog.get(oid)
        if unis and o is not None and not o.is_vsosh:
            ranked.append((o, profile, unis))
    # сначала профили, которые уже есть в плане (информатика, математика…), потом остальные
    plan_profiles = {p for _, p in taken}
    ranked.sort(key=lambda x: (x[1] not in plan_profiles, -len(x[2]), x[0].best_level(catalog.current_year) or 9,
                               x[0].name))
    return ranked[:limit]


def status_message(o_name: str, profile: str, status: str, site_url: str = "") -> str:
    """Текст уведомления клиенту о смене статуса (HTML для Telegram)."""
    label = STATUSES[status][0]
    head = {
        "registration": "Пора регистрироваться",
        "registered": "Ты зарегистрирован(а)",
        "passed": "Отбор пройден!",
        "failed": "Отбор, к сожалению, не пройден",
        "final": "Ты в финале",
        "winner": "Победа!",
        "prize": "Диплом призёра!",
    }.get(status, "Обновление по сопровождению")
    name = escape(o_name) + (f", {escape(profile)}" if profile else "")
    text = f"<b>{head}</b>\n{name} — {escape(label.lower())}."
    if site_url:
        text += f'\n\n<a href="{escape(site_url)}/me/roadmap">Открыть роадмап</a>'
    return text
