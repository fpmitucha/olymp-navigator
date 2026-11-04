"""Новости Бвишечки: лента о регистрациях и этапах олимпиад со льготами в вузах.

Черновики собираются сами из этапов, которые бот раз в сутки забирает с olimpiada.ru:
  • открылась регистрация;
  • до конца регистрации 3 дня;
  • появились даты отборочного или заключительного этапа;
  • по понедельникам — подборка недели.
Текст пишем своими словами (факты и даты), источник — ссылкой. В новость берём только олимпиады,
которые дают льготы хоть в одном из собранных вузов, и пишем, где призёру дают БВИ или 100 баллов.
Куратор проверяет черновики в админке; опубликованные уходят на сайт (/news) и в Telegram-канал.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape

from app.benefits import BENEFIT_TYPES, BenefitsCatalog, fitting, profile_level
from app.catalog import Catalog, CatalogOlympiad

KINDS = {
    "registration_open": "Открылась регистрация",
    "registration_closing": "Скоро конец регистрации",
    "stage_dates": "Даты этапа",
    "digest": "Подборка недели",
    "article": "Статья",
}
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]
BENEFIT_WORDS = {"bvi": "БВИ", "score100": "100 баллов ЕГЭ", "dvi100": "100 баллов за ДВИ"}
CATEGORIES = {
    "stages": "Этапы и регистрации",
    "benefits": "Льготы вузов",
    "digest": "Подборки",
    "guides": "Гайды",
    "project": "Новости Бвишечки",
}
KIND_CATEGORY = {"registration_open": "stages", "registration_closing": "stages", "stage_dates": "stages",
                 "digest": "digest", "article": "guides", "benefits": "benefits"}
CTA = {
    "bot": "Подпишись на олимпиаду в боте Бвишечки — напомним о регистрации и каждом этапе.",
    "help": "Хочешь, чтобы за регистрациями и этапами следил куратор, — оставь заявку на сопровождение на сайте.",
    "route": "Какие олимпиады ведут в твой вуз, смотри в разделе «Подбор» на сайте.",
}
# агрегаторы — не официальные источники
NOT_OFFICIAL = ("olimpiada.ru", "olimpiada.ru/", "vk.com", "t.me")


def day(d: date | None) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}" if d else "дата уточняется"


def span(start: date | None, end: date | None) -> str:
    if start and end and start != end:
        if start.month == end.month:
            return f"{start.day}–{end.day} {MONTHS[end.month - 1]}"
        return f"{day(start)} — {day(end)}"
    return day(start or end)


@dataclass
class Draft:
    kind: str
    key: str                 # чтобы одна и та же новость не появлялась дважды
    title: str
    body: str
    olympiad_id: str | None = None
    profile: str = ""
    source_url: str | None = None
    event_date: date | None = None


def benefits_line(benefits: BenefitsCatalog, o: CatalogOlympiad, profile_label: str, year: str,
                  diploma: str = "prize", grade: int = 11, universities: set[str] | None = None) -> tuple[str, int]:
    """«Призёрам: БВИ — МАИ, МТУСИ; 100 баллов ЕГЭ — РУДН» и число вузов с льготами.
    universities — показать только эти вузы (пусто — все собранные)."""
    profiles = [p.strip() for p in (profile_label or "").split(",") if p.strip() in o.profiles] or \
        sorted(o.profiles_in(year))
    best: dict[str, str] = {}
    for profile in profiles:
        level, _ = profile_level(o, profile, year)
        for u, rules in fitting(benefits, o.id, profile, diploma, grade, level):
            if universities and u.university not in universities:
                continue
            kind = min((r.benefit for r in rules), key=list(BENEFIT_TYPES).index)
            old = best.get(u.university)
            if old is None or list(BENEFIT_TYPES).index(kind) < list(BENEFIT_TYPES).index(old):
                best[u.university] = kind
    if not best:
        return "", 0
    parts = []
    for kind in BENEFIT_TYPES:
        unis = sorted(u for u, k in best.items() if k == kind)
        if unis:
            parts.append(f"{BENEFIT_WORDS[kind]} — {', '.join(unis)}")
    who = ("Победителям" if diploma == "winner" else "Призёрам") + f" ({grade} класс"
    who += ")" if profile_label else "; зависит от профиля)"
    return f"{who}: " + "; ".join(parts) + ".", len(best)


def _official_url(o: CatalogOlympiad) -> str | None:
    found = [u for u in o.websites if is_official(u)]
    return found[0] if found else None


def _name(o: CatalogOlympiad) -> str:
    from app.bot.texts import button_name

    return button_name(o, 90)


def _about(o: CatalogOlympiad, profile: str) -> str:
    return _name(o) + (f" ({profile})" if profile else "")


def generate(catalog: Catalog, benefits: BenefitsCatalog, stages, today: date, since: str) -> list[Draft]:
    """Черновики по этапам. since — ISO-время запуска ленты: этапы, загруженные раньше,
    не превращаем в «новые даты» (иначе первая синхронизация завалит очередь)."""
    year = catalog.current_year
    drafts: list[Draft] = []
    lines: dict[tuple[str, str], tuple[str, int]] = {}
    for st in stages:
        if st.kind not in ("registration", "qualifying", "final") or not st.is_upcoming(today):
            continue
        o = catalog.get(st.olympiad_id)
        if o is None or not benefits.for_olympiad(o.id):
            continue
        key = (o.id, st.profile or "")
        if key not in lines:
            lines[key] = benefits_line(benefits, o, st.profile or "", year)
        line, count = lines[key]
        if not count:
            continue
        profile = st.profile or ""
        about = _about(o, profile)
        tail = f"\n\n{line}" if line else ""
        if st.kind == "registration":
            end = st.ends_at
            # открылась за последнюю неделю (повторов не будет — ключ новости уникален)
            if st.starts_at and 0 <= (today - st.starts_at).days <= 7 and (end is None or end >= today):
                drafts.append(Draft(
                    "registration_open", f"reg_open:{o.id}:{profile}:{end}",
                    f"Открылась регистрация: {about}",
                    f"Регистрация открыта{' до ' + day(end) if end else ''}. Зарегистрируйся заранее — "
                    f"в последние дни сайты олимпиад часто перегружены.{tail}",
                    o.id, profile, _official_url(o), end))
            if end and (end - today).days == 3:
                drafts.append(Draft(
                    "registration_closing", f"reg_close:{o.id}:{profile}:{end}",
                    f"Осталось 3 дня регистрации: {about}",
                    f"Регистрация закрывается {day(end)}. Если ещё не зарегистрировался — сейчас самое время.{tail}",
                    o.id, profile, _official_url(o), end))
        elif st.kind in ("qualifying", "final") and (st.created_at or "") >= since and st.is_upcoming(today):
            what = "отборочного" if st.kind == "qualifying" else "заключительного"
            drafts.append(Draft(
                "stage_dates", f"dates:{o.id}:{profile}:{st.kind}:{st.title.lower()}:{st.starts_at}",
                f"Даты {what} этапа: {about}",
                f"{st.title}: {span(st.starts_at, st.ends_at)}.{tail}",
                o.id, profile, _official_url(o), st.starts_at or st.ends_at))
    return drafts


def weekly_digest(catalog: Catalog, benefits: BenefitsCatalog, stages, today: date) -> Draft | None:
    """Подборка по понедельникам: что закрывается и что проходит на этой неделе."""
    if today.weekday() != 0:
        return None
    week_end = today + timedelta(days=6)
    closing, events = [], []
    seen = set()
    for st in stages:
        o = catalog.get(st.olympiad_id)
        if o is None or not benefits.for_olympiad(o.id):
            continue
        key = (o.id, st.kind, st.title, st.starts_at, st.ends_at)
        if key in seen:
            continue
        seen.add(key)
        about = _about(o, st.profile or "")
        if st.kind == "registration" and st.ends_at and today <= st.ends_at <= week_end:
            closing.append((st.ends_at, f"• {about} — до {day(st.ends_at)}"))
        elif st.kind in ("qualifying", "final") and st.starts_at and today <= st.starts_at <= week_end:
            events.append((st.starts_at, f"• {about}: {st.title.lower()} — {span(st.starts_at, st.ends_at)}"))
    if not closing and not events:
        return None
    parts = []
    if closing:
        parts.append("Заканчивается регистрация:\n" + "\n".join(t for _, t in sorted(closing)[:25]))
    if events:
        parts.append("Проходят этапы:\n" + "\n".join(t for _, t in sorted(events)[:25]))
    return Draft("digest", f"digest:{today}", f"Олимпиады на неделе {day(today)} — {day(week_end)}",
                 "\n\n".join(parts), event_date=today)


def slugify(text: str) -> str:
    from app.export_pdf import _translit

    return re.sub(r"[^a-z0-9]+", "-", _translit(text.lower())).strip("-")[:70] or "news"


def channel_text(item: dict, site_url: str) -> str:
    """Пост для Telegram-канала (HTML): заголовок, текст и ссылка на новость на сайте."""
    body = escape(item["body"])
    if len(body) > 3200:
        body = body[:3200].rsplit("\n", 1)[0] + "\n…"
    text = f"<b>{escape(item['title'])}</b>\n\n{body}"
    if site_url:
        text += f'\n\n<a href="{escape(site_url)}/news/{item["id"]}-{slugify(item["title"])}">Подробнее на сайте</a>'
    elif item.get("source_url"):
        text += f'\n\n<a href="{escape(item["source_url"])}">Источник</a>'
    return text


# ---------------------------------------------------------------------------
# Источники: официальные сайты и документы, ссылки проверяем
# ---------------------------------------------------------------------------

def is_official(url: str) -> bool:
    host = url.split("//", 1)[-1].split("/", 1)[0].lower().removeprefix("www.")
    return bool(host) and not any(host == x or host.endswith("." + x) for x in NOT_OFFICIAL)


def official_sources(o: CatalogOlympiad | None) -> list[dict]:
    """Официальные сайты олимпиады из перечня РСОШ (не агрегаторы)."""
    if o is None:
        return []
    return [{"title": "Официальный сайт олимпиады", "url": u} for u in o.websites if is_official(u)][:2]


def check_url(url: str, timeout: float = 8) -> bool:
    """Ссылка открывается: ответ без ошибки (меньше 400) после переадресаций."""
    import requests

    if not url.startswith(("http://", "https://")):
        return False
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BvishechkaLinkCheck/1.0; +https://bvishechka.com)"}
    try:
        with requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True) as r:
            return r.status_code < 400
    except Exception:  # noqa: BLE001 — сеть, сертификат, таймаут: ссылка не рабочая
        return False


def check_sources(sources: list[dict], checker=check_url) -> list[dict]:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return [dict(src, ok=checker(src["url"]), checked_at=now) for src in sources]


def parse_sources(text: str) -> list[dict]:
    """Строки «https://… — подпись» → [{"url", "title"}]."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        url, _, title = line.partition(" ")
        title = title.strip().lstrip("—-– ").strip()
        if url.startswith(("http://", "https://")):
            out.append({"url": url[:500], "title": title[:150] or "Источник"})
    return out[:10]


def sources_text(sources: list[dict]) -> str:
    return "\n".join(f"{s['url']} — {s['title']}" for s in sources)


# ---------------------------------------------------------------------------
# Генератор новостей из админки
# ---------------------------------------------------------------------------

@dataclass
class GenOptions:
    benefits: bool = True
    diploma: str = "prize"
    grade: int = 11
    universities: set[str] | None = None
    cta: tuple[str, ...] = ("bot",)


def _cta(opts: GenOptions) -> str:
    return "\n\n".join(CTA[c] for c in opts.cta if c in CTA)


def _join(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)


def stage_news(catalog: Catalog, benefits: BenefitsCatalog, stages, olympiad_ids: list[str], profiles: list[str],
               kinds: set[str], start: date, end: date, opts: GenOptions, combined: bool) -> list[Draft]:
    """Новости о датах этапов выбранных олимпиад за период: по одной на олимпиаду или одна общая."""
    from app.crm import stage_fits

    year = catalog.current_year
    chosen = set(olympiad_ids)
    by_olympiad: dict[str, list] = {}
    for st in stages:
        if st.kind not in kinds or (chosen and st.olympiad_id not in chosen):
            continue
        if profiles and not any(stage_fits(st.profile, p) for p in profiles):
            continue
        first, last = st.starts_at or st.ends_at, st.ends_at or st.starts_at
        if first is None or last < start or first > end:
            continue
        by_olympiad.setdefault(st.olympiad_id, []).append(st)
    drafts = []
    blocks = []
    for oid, items in by_olympiad.items():
        o = catalog.get(oid)
        if o is None:
            continue
        seen = set()
        lines = []
        for st in sorted(items, key=lambda x: (x.starts_at or x.ends_at)):
            key = (st.kind, st.title.lower(), st.starts_at, st.ends_at)
            if key in seen:
                continue
            seen.add(key)
            prof = f" ({st.profile})" if st.profile and not profiles else ""
            when = f"до {day(st.ends_at)}" if st.kind == "registration" and st.ends_at else span(st.starts_at, st.ends_at)
            lines.append(f"• {st.title}{prof}: {when}")
        label = ", ".join(profiles) if profiles else ""
        line = benefits_line(benefits, o, label, year, opts.diploma, opts.grade, opts.universities)[0] \
            if opts.benefits else ""
        name = _name(o) + (f" ({label})" if label else "")
        if combined:
            blocks.append((min(x.starts_at or x.ends_at for x in items), f"{name}\n" + "\n".join(lines)
                           + (f"\n{line}" if line else ""), o))
        else:
            what = "регистрация и этапы" if "registration" in {x.kind for x in items} else "даты этапов"
            drafts.append(Draft("stage_dates", "", f"{name}: {what}", _join("\n".join(lines), line, _cta(opts)),
                                o.id, label, None, min(x.starts_at or x.ends_at for x in items)))
    if combined and blocks:
        blocks.sort(key=lambda b: b[0])
        title = f"Олимпиады: даты с {day(start)} по {day(end)}"
        body = _join(*(b[1] for b in blocks), _cta(opts))
        d = Draft("digest", "", title, body, event_date=start)
        d.olympiad_id = blocks[0][2].id if len(blocks) == 1 else None
        drafts.append(d)
    return drafts


def benefits_news(catalog: Catalog, benefits: BenefitsCatalog, university: str, program: str, diploma: str,
                  olympiad_ids: list[str], opts: GenOptions, limit: int = 40) -> Draft | None:
    """«МАИ: какие олимпиады дают БВИ и 100 баллов» — по документу вуза."""
    from app.routes import route

    u = benefits.get(university)
    if u is None:
        return None
    items = route(u, program, diploma, catalog.current_year)
    if olympiad_ids:
        items = [it for it in items if it.olympiad.id in set(olympiad_ids)]
    if not items:
        return None
    groups: dict[str, list[str]] = {}
    for it in items[:limit]:
        for kind in BENEFIT_WORDS:
            profs = [p.profile for p in it.profiles if p.benefit == kind]
            if profs:
                groups.setdefault(kind, []).append(f"• {_name(it.olympiad)}: {', '.join(profs[:4])}"
                                                   + (" и др." if len(profs) > 4 else ""))
    who = "победителям" if diploma == "winner" else "призёрам"
    parts = [f"{u.full_name or u.university} — льготы {who} олимпиад"
             + (f" на программу {program}" if program else "") + f" (правила приёма {u.admission_year} года)."]
    for kind, lines in groups.items():
        parts.append(f"{BENEFIT_WORDS[kind]}:\n" + "\n".join(lines))
    scores = sorted({p.min_score for it in items for p in it.profiles if p.min_score})
    if scores:
        parts.append(f"Льготу нужно подтвердить ЕГЭ по профильному предмету: от {scores[0]} баллов.")
    if len(items) > limit:
        parts.append(f"И ещё {len(items) - limit} олимпиад — полный список на странице вуза.")
    title = f"{u.university}: какие олимпиады дают льготы" + (f" на «{program}»" if program else "")
    d = Draft("benefits", "", title, _join(*parts, _cta(opts)), event_date=None)
    d.source_url = u.source_url
    return d


def digest_news(catalog: Catalog, benefits: BenefitsCatalog, stages, start: date, end: date, level: int,
                profiles: list[str], opts: GenOptions) -> list[Draft]:
    """Подборка за период по всем олимпиадам (с фильтрами по уровню и профилю), у которых есть льготы."""
    year = catalog.current_year
    ids = []
    for o in catalog.olympiads.values():
        if not benefits.for_olympiad(o.id):
            continue
        if level and o.best_level(year) != level:
            continue
        if opts.universities and not benefits_line(benefits, o, ", ".join(profiles), year, opts.diploma,
                                                   opts.grade, opts.universities)[1]:
            continue
        ids.append(o.id)
    if not ids:
        return []
    return stage_news(catalog, benefits, stages, ids, profiles, {"registration", "qualifying", "final"},
                      start, end, opts, combined=True)
