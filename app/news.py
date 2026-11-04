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


def benefits_line(benefits: BenefitsCatalog, o: CatalogOlympiad, profile_label: str, year: str) -> tuple[str, int]:
    """«Призёрам: БВИ — МАИ, МТУСИ; 100 баллов ЕГЭ — РУДН» и число вузов с льготами."""
    profiles = [p.strip() for p in (profile_label or "").split(",") if p.strip() in o.profiles] or \
        sorted(o.profiles_in(year))
    best: dict[str, str] = {}
    for profile in profiles:
        level, _ = profile_level(o, profile, year)
        for u, rules in fitting(benefits, o.id, profile, "prize", 11, level):
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
    who = "Призёрам (11 класс)" if profile_label else "Призёрам (11 класс; зависит от профиля)"
    return f"{who}: " + "; ".join(parts) + ".", len(best)


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
                    o.id, profile, st.source_url, end))
            if end and (end - today).days == 3:
                drafts.append(Draft(
                    "registration_closing", f"reg_close:{o.id}:{profile}:{end}",
                    f"Осталось 3 дня регистрации: {about}",
                    f"Регистрация закрывается {day(end)}. Если ещё не зарегистрировался — сейчас самое время.{tail}",
                    o.id, profile, st.source_url, end))
        elif st.kind in ("qualifying", "final") and (st.created_at or "") >= since and st.is_upcoming(today):
            what = "отборочного" if st.kind == "qualifying" else "заключительного"
            drafts.append(Draft(
                "stage_dates", f"dates:{o.id}:{profile}:{st.kind}:{st.title.lower()}:{st.starts_at}",
                f"Даты {what} этапа: {about}",
                f"{st.title}: {span(st.starts_at, st.ends_at)}.{tail}",
                o.id, profile, st.source_url, st.starts_at or st.ends_at))
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
