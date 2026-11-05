"""Сопровождение (CRM): статусы олимпиад клиента, его роадмап и тексты уведомлений.

Куратор включает сопровождение по короткому ID из кабинета, ставит цели (вуз + программа) и олимпиады.
Клиент видит роадмап: по каждой олимпиаде шаги «регистрация → отборочные этапы → финал → итог»
с датами с сайта олимпиады и какие льготы она даёт в его вузах. Отборочных этапов бывает несколько
(у «Высшей пробы» два) — их число и даты берутся из этапов, загруженных с olimpiada.ru.
Смена статуса уходит клиенту в Telegram через очередь outbox.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from html import escape

from app.benefits import BENEFIT_TYPES, BenefitsCatalog, profile_level
from app.catalog import Catalog, CatalogOlympiad
from app.routes import route

BASE_STATUSES = {
    "todo": "Не начато",
    "registration": "Нужна регистрация",
    "registered": "Регистрация пройдена",
    "final": "Идёт финал",
    "winner": "Победитель",
    "prize": "Призёр",
    "participant": "Участник финала",
}
ALIASES = {"qualifying": "q1", "failed": "f1"}   # статусы первой версии
DEADLINE_STATUSES = {"todo", "registration"}   # пока не зарегистрированы — следим за концом регистрации
RESULT_STATUSES = {"winner", "prize", "participant"}
BENEFIT_SHORT = {"bvi": "БВИ", "score100": "100 баллов", "dvi100": "100 за ДВИ"}
ACCESS_DAYS_BEFORE = 3     # доступ к кабинету нужен за 3 дня до конца регистрации — куратору на проверку
ACCESS_LABELS = {"pending": "На проверке", "ok": "Проверено", "bad": "Не подходит — введи заново"}
PLAN_KINDS = ("registration", "qualifying", "final")
_QF = re.compile(r"([qf])(\d{1,2})(ok)?")


def valid_status(status: str) -> bool:
    if status in BASE_STATUSES or status in ALIASES or status == "passed":
        return True
    m = _QF.fullmatch(status)
    return bool(m) and 1 <= int(m.group(2)) <= 20 and not (m.group(1) == "f" and m.group(3))


def progress(status: str, n: int) -> tuple[int, int | None, int | None]:
    """(последний сделанный шаг, текущий шаг, не пройденный шаг). Шаги: 0 — регистрация,
    1..n — отборочные, n+1 — финал, n+2 — итог."""
    s = ALIASES.get(status, status)
    if s == "registration":
        return -1, 0, None
    if s == "registered":
        return 0, None, None
    if s == "passed":
        return n, None, None
    if s == "final":
        return n, n + 1, None
    if s in RESULT_STATUSES:
        return n + 2, None, None
    m = _QF.fullmatch(s)
    if m:
        i = min(int(m.group(2)), n)
        if m.group(1) == "f":
            return i - 1, None, i
        return (i, None, None) if m.group(3) else (i - 1, i, None)
    return -1, None, None


def status_label(status: str, n: int) -> str:
    s = ALIASES.get(status, status)
    if s in BASE_STATUSES:
        return BASE_STATUSES[s]
    if s == "passed":
        return "Отбор пройден"
    m = _QF.fullmatch(s)
    if not m:
        return BASE_STATUSES["todo"]
    i = min(int(m.group(2)), n)
    stage = "отбор" if n == 1 else f"{i}-й отборочный"
    if m.group(1) == "f":
        return f"{stage[:1].upper()}{stage[1:]} не пройден"
    if m.group(3):
        return "Отбор пройден" if i >= n else f"{i}-й отборочный пройден"
    return f"Идёт {stage}"


def status_options(n: int) -> list[tuple[str, str]]:
    keys = ["todo", "registration", "registered"]
    for i in range(1, n + 1):
        keys += [f"q{i}", f"q{i}ok", f"f{i}"]
    keys += ["final", "winner", "prize", "participant"]
    return [(k, status_label(k, n)) for k in keys]


def normalize_status(status: str, n: int) -> str:
    """Старые статусы → новые ключи (чтобы в списке куратора было выбрано верное)."""
    s = ALIASES.get(status, status)
    return f"q{n}ok" if s == "passed" else s


def stage_fits(stage_profile: str | None, profile: str) -> bool:
    """Этап относится к профилю: общий этап олимпиады или страница этого предмета («математика, физика»)."""
    if not stage_profile or not profile:
        return True
    a, b = stage_profile.lower(), profile.lower()
    return b in a or a in b


def _fmt(d: date | None) -> str:
    return d.strftime("%d.%m") if d else ""


def _when(start: date | None, end: date | None) -> str:
    if start and end and start != end:
        return f"{_fmt(start)}–{_fmt(end)}"
    return _fmt(start or end)


@dataclass
class Step:
    name: str
    state: str          # d — сделано, n — в работе, x — впереди, f — не пройден, s — пропущен
    when: str = ""      # даты этапа с сайта олимпиады


@dataclass
class RoadmapItem:
    row_id: int
    olympiad: CatalogOlympiad
    profile: str
    status: str
    deadline: date | None          # конец регистрации: вручную или с сайта олимпиады
    deadline_manual: bool
    plan: list[dict] = field(default_factory=list)       # этапы олимпиады (снимок, прошедшие не пропадают)
    benefits: list[tuple[str, str, str]] = field(default_factory=list)  # (вуз, льгота, кому: prize | winner)
    access: str | None = None      # доступ к кабинету: None | pending | ok | bad
    plan_changed: str | None = None   # новый снимок этапов, если его нужно сохранить

    @property
    def quals(self) -> list[dict]:
        found = [e for e in self.plan if e["kind"] == "qualifying"]
        return sorted(found, key=lambda e: e.get("start") or e.get("end") or "")

    @property
    def n(self) -> int:
        return max(1, len(self.quals))

    @property
    def label(self) -> str:
        return status_label(self.status, self.n)

    @property
    def status_key(self) -> str:
        return normalize_status(self.status, self.n)

    @property
    def options(self) -> list[tuple[str, str]]:
        return status_options(self.n)

    @property
    def steps(self) -> list[Step]:
        n, quals = self.n, self.quals
        finals = [e for e in self.plan if e["kind"] == "final"]
        names = ["Регистрация"] + (["Отборочный"] if n == 1 else [f"{i}-й отборочный" for i in range(1, n + 1)])
        names += ["Финал", "Итог"]
        whens = [f"до {_fmt(self.deadline)}" if self.deadline and self.status in DEADLINE_STATUSES else ""]
        whens += [_when(_d(q.get("start")), _d(q.get("end"))) for q in quals] or [""]
        whens += [_when(_d(finals[0].get("start")), _d(finals[0].get("end"))) if finals else "", ""]
        done, current, failed = progress(self.status, n)
        steps = []
        for i, (name, when) in enumerate(zip(names, whens)):
            if failed is not None and i == failed:
                state = "f"
            elif failed is not None and i > failed:
                state = "s"
            elif i <= done:
                state = "d"
            elif i == current:
                state = "n"
            else:
                state = "x"
            steps.append(Step(name, state, when))
        return steps

    @property
    def tone(self) -> str:
        """Цвет плашки статуса: alert — от клиента что-то нужно, gold — успех, plain — остальное."""
        if self.status == "registration" or self.access == "bad":
            return "alert"
        s = self.status_key
        if s in ("winner", "prize") or (s.startswith("q") and s.endswith("ok")):
            return "gold"
        return "plain"

    @property
    def access_deadline(self) -> date | None:
        return self.deadline - timedelta(days=ACCESS_DAYS_BEFORE) if self.deadline else None

    @property
    def needs_access(self) -> bool:
        """Клиенту стоит ввести доступ: ещё не зарегистрированы, а доступа нет или он не подошёл."""
        return self.status in DEADLINE_STATUSES and self.access in (None, "bad")


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


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


def merge_plan(plan_json: str, stages, profile: str) -> tuple[list[dict], bool]:
    """Снимок этапов + этапы из базы. С сайта прошедшие этапы пропадают, а в роадмапе они нужны —
    поэтому храним снимок и только дополняем его (новые этапы, уточнённые даты)."""
    try:
        plan = json.loads(plan_json or "[]")
    except ValueError:
        plan = []
    # Ключ этапа — его id на сайте олимпиады; у одинаково названных этапов («Отборочный этап»
    # дважды, с разными датами) ключ по названию склеивал их в один — и второй отбор терялся.
    def key_of(e: dict) -> tuple:
        return ("id", e["id"]) if e.get("id") else (e["kind"], e["title"].lower())

    by_key = {key_of(e): e for e in plan}
    changed = False
    claimed: set[tuple] = set()
    for st in stages:
        if st.kind not in PLAN_KINDS or not stage_fits(st.profile, profile):
            continue
        entry = {"kind": st.kind, "title": st.title,
                 "start": st.starts_at.isoformat() if st.starts_at else None,
                 "end": st.ends_at.isoformat() if st.ends_at else None}
        sid = getattr(st, "external_id", None) or (f"db{st.id}" if getattr(st, "id", None) else None)
        if sid:
            entry["id"] = sid
            old = by_key.get((st.kind, st.title.lower()))
            if old is not None and not old.get("id") and (st.kind, st.title.lower()) not in claimed:
                del by_key[(st.kind, st.title.lower())]   # запись из старого снимка без id — обновляем её
            claimed.add((st.kind, st.title.lower()))
        key = key_of(entry)
        if by_key.get(key) != entry:
            by_key[key] = entry
            changed = True
    return list(by_key.values()), changed


def registration_deadline(plan: list[dict], today: date) -> date | None:
    """Ближайший конец регистрации из этапов олимпиады."""
    found = [_d(e.get("end") or e.get("start")) for e in plan if e["kind"] == "registration"]
    found = [d for d in found if d and d >= today]
    return min(found) if found else None


def build_roadmap(catalog: Catalog, benefits: BenefitsCatalog, goals: list[dict], rows: list[dict],
                  stages_for, today: date, accesses: dict[int, str] | None = None) -> Roadmap:
    """stages_for(olympiad_id) → этапы олимпиады из базы; accesses — {строка олимпиады: статус доступа}."""
    year = catalog.current_year
    by_pair = goal_benefits(benefits, goals, year)
    items = []
    for r in rows:
        o = catalog.get(r["olympiad_id"])
        if o is None:
            continue
        plan, changed = merge_plan(r.get("plan") or "[]", stages_for(o.id), r["profile"])
        items.append(RoadmapItem(
            row_id=r["id"], olympiad=o, profile=r["profile"], status=r["status"],
            deadline=r["reg_deadline"] or registration_deadline(plan, today),
            deadline_manual=r["reg_deadline"] is not None, plan=plan,
            benefits=sorted(by_pair.get((o.id, r["profile"]), []), key=lambda x: (_rank(x[1]), x[0])),
            access=(accesses or {}).get(r["id"]),
            plan_changed=json.dumps(plan, ensure_ascii=False) if changed else None))
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


@dataclass
class Pick:
    """Олимпиада автоподбора: что она даёт в вузах-целях клиента."""
    olympiad: CatalogOlympiad
    profile: str
    level: int | None
    benefits: list[tuple[str, str, str]]     # (вуз, льгота, prize | winner), лучшая по каждому вузу

    @property
    def key(self) -> str:
        return f"{self.olympiad.id}|{self.profile}"

    @property
    def bvi_unis(self) -> list[str]:
        return sorted(u for u, b, _ in self.benefits if b == "bvi")


def auto_pick(catalog: Catalog, benefits: BenefitsCatalog, goals: list[dict],
              taken: set[tuple[str, str]]) -> dict[str, list[Pick]]:
    """Автоподбор под цели: {"bvi": [...], "score100": [...]}.

    БВИ — олимпиады, которые дают БВИ хотя бы в одном вузе-цели; 100 баллов — остальные,
    что дают 100 за ЕГЭ или ДВИ. ВсОШ не предлагаем: её финал даёт БВИ везде, её ведут отдельно.
    Сначала — где льгота и призёру, больше вузов-целей и выше уровень олимпиады.
    """
    year = catalog.current_year
    out: dict[str, list[Pick]] = {"bvi": [], "score100": []}
    for (oid, profile), found in goal_benefits(benefits, goals, year).items():
        o = catalog.get(oid)
        if o is None or o.is_vsosh or (oid, profile) in taken:
            continue
        level, _ = profile_level(o, profile, year)
        found = sorted(found, key=lambda x: (_rank(x[1]), x[2] != "prize", x[0]))
        group = "bvi" if any(b == "bvi" for _, b, _ in found) else "score100"
        out[group].append(Pick(o, profile, level, found))
    for group, items in out.items():
        def score(p: Pick, group=group):
            main = [x for x in p.benefits if (x[1] == "bvi") == (group == "bvi")]
            return (-len(main), -sum(d == "prize" for _, _, d in main), p.level or 9, p.olympiad.name, p.profile)
        items.sort(key=score)
    return out


def plan_added_message(names: list[str], site_url: str = "") -> str:
    """Одно сообщение клиенту на всю пачку одобренных олимпиад."""
    head = "В план добавлена олимпиада" if len(names) == 1 else f"В план добавлено олимпиад: {len(names)}"
    lines = "\n".join(f"• {escape(n)}" for n in names[:15])
    more = f"\n…и ещё {len(names) - 15}" if len(names) > 15 else ""
    return f"<b>{head}</b>\n{lines}{more}\nНапомним о регистрации и этапах." + _link(site_url)


def _link(site_url: str) -> str:
    return f'\n\n<a href="{escape(site_url)}/me/roadmap">Открыть роадмап</a>' if site_url else ""


def status_message(o_name: str, profile: str, status: str, n: int = 1, site_url: str = "") -> str:
    """Текст уведомления клиенту о смене статуса (HTML для Telegram)."""
    s = normalize_status(status, n)
    m = _QF.fullmatch(s)
    if s in BASE_STATUSES:
        head = {"registration": "Пора регистрироваться", "registered": "Ты зарегистрирован(а)",
                "final": "Ты в финале", "winner": "Победа!", "prize": "Диплом призёра!"}.get(s, "Обновление")
    elif m and m.group(1) == "f":
        head = "Отбор, к сожалению, не пройден"
    elif m and m.group(3):
        head = "Отбор пройден!" if int(m.group(2)) >= n else "Этап пройден!"
    else:
        head = "Начался отборочный этап"
    name = escape(o_name) + (f", {escape(profile)}" if profile else "")
    return f"<b>{head}</b>\n{name} — {escape(status_label(s, n).lower())}." + _link(site_url)


def access_message(o_name: str, ok: bool, site_url: str = "") -> str:
    if ok:
        return f"<b>Доступ проверен</b>\nВход в кабинет «{escape(o_name)}» работает — дальше мы всё сделаем сами."
    return (f"<b>Доступ не подошёл</b>\nНе получилось войти в кабинет «{escape(o_name)}». "
            "Проверь логин и пароль и введи их заново." + _link(site_url))


def access_reminders(catalog: Catalog, rows: list[dict], stages_for, today: date, site_url: str = ""):
    """Напоминания ввести доступ к кабинету: за 3 дня до срока, накануне и в последний день.
    Возвращает [(строка олимпиады, telegram_id, вид, текст)]."""
    out = []
    for r in rows:
        if r["status"] not in DEADLINE_STATUSES or r["access"] in ("pending", "ok"):
            continue
        o = catalog.get(r["olympiad_id"])
        if o is None:
            continue
        plan, _ = merge_plan(r.get("plan") or "[]", stages_for(o.id), r["profile"])
        deadline = r["reg_deadline"] or registration_deadline(plan, today)
        if deadline is None:
            continue
        due = deadline - timedelta(days=ACCESS_DAYS_BEFORE)
        days = (due - today).days
        kind = {3: "access_t3", 1: "access_t1", 0: "access_t0"}.get(days)
        if kind is None:
            continue
        when = {3: "через 3 дня", 1: "завтра", 0: "сегодня"}[days]
        name = o.name if len(o.name) < 60 else o.name[:57] + "…"
        text = (f"<b>Нужен доступ к кабинету олимпиады</b>\n«{escape(name)}»: введи логин и пароль от кабинета "
                f"{when} ({due:%d.%m}) — регистрация закрывается {deadline:%d.%m}." + _link(site_url))
        out.append((r["id"], r["telegram_id"], kind, text))
    return out
