"""Тексты сообщений бота (HTML-разметка Telegram). Без зависимостей от aiogram — легко тестировать."""

from __future__ import annotations

import re
from datetime import date
from html import escape

from app.catalog import CatalogOlympiad, SearchHit, year_start
from app.stages import Reminder, Stage

MAX_MESSAGE = 3900  # лимит Telegram 4096 символов, оставляем запас

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

START = (
    "👋 Привет! Я помогаю разобраться с олимпиадами из перечня РСОШ и ВсОШ.\n\n"
    "🔍 <b>Напиши название, сокращение или предмет</b> — например, <code>Физтех</code>, "
    "<code>НТО</code> или <code>информатика</code>. Можно вместе: <code>ломоносов химия</code>.\n\n"
    "📚 Или открой перечень по уровням — кнопки ниже.\n\n"
    "Подпишись на олимпиаду — напомню о регистрации и этапах."
)

HELP = (
    "<b>Как пользоваться</b>\n\n"
    "🔍 <b>Поиск.</b> Название олимпиады, сокращение (ВсОШ, МОШ, НТО, ВШЭ, МГУ, СПбГУ, ОММО) или предмет.\n\n"
    "📋 <b>Карточка.</b> Профили по уровням и что изменилось с прошлого года. "
    "Кнопка «Подробнее» — предметы и уровни каждого профиля по годам.\n\n"
    "📆 <b>Почему важны прошлые годы.</b> Диплом действует 4 года, а вуз смотрит на уровень олимпиады "
    "в том году, когда диплом получен.\n\n"
    "🔔 <b>Подписки.</b> Напомню о регистрации за 3 дня и в последний день, об этапах — за неделю, "
    "за день и в день этапа. Пишу только с 9:00 до 21:00 по Москве.\n\n"
    "<i>Данные — с сайта Российского совета олимпиад школьников. Окончательную информацию "
    "сверяй на сайтах олимпиады и вуза.</i>"
)

NOT_FOUND = (
    "🤷 По запросу «{query}» ничего не нашлось.\n\n"
    "Попробуй короче: одно слово из названия, сокращение (МОШ, НТО, ВШЭ) или предмет."
)

LEVEL_ICONS = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣"}
MAX_DROPPED = 6           # сколько профилей прошлых лет показывать в краткой карточке
DETAIL_SUBJECTS_UP_TO = 12  # в подробной карточке предметы показываем, если профилей не больше


def ru_date(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}"


def ru_period(start: date | None, end: date | None) -> str:
    if start and end and start != end:
        if start.month == end.month and start.year == end.year:
            return f"{start.day}–{end.day} {MONTHS[end.month - 1]}"
        return f"{ru_date(start)} – {ru_date(end)}"
    d = start or end
    return ru_date(d) if d else "дата уточняется"


def days_phrase(days: int) -> str:
    if days == 0:
        return "сегодня"
    if days == 1:
        return "завтра"
    if days % 10 == 1 and days % 100 != 11:
        word = "день"
    elif days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
        word = "дня"
    else:
        word = "дней"
    return f"через {days} {word}"


# Короткие имена для длинных названий без «кавычек»: подстрока названия -> подпись
KNOWN_SHORT_NAMES: dict[str, str] = {
    "санкт-петербургского государственного университета": "Олимпиада СПбГУ",
    "российской академии народного хозяйства": "Олимпиада РАНХиГС",
    "открытая региональная межвузовская олимпиада": "ОРМО (ТГУ)",
    "объединенная межвузовская олимпиада": "ОММО",
    "объединённая межвузовская олимпиада": "ОММО",
    "санкт-петербургская олимпиада школьников": "Санкт-Петербургская олимпиада",
    "казанский (приволжский) федеральный университет": "Олимпиады КФУ",
}


def short_name(name: str, limit: int = 48) -> str:
    """Короткое имя для кнопок: известное сокращение, текст в последних «кавычках»,
    «Олимпиада имени …» или обрезанное название."""
    lowered = name.lower()
    for fragment, label in KNOWN_SHORT_NAMES.items():
        if fragment in lowered:
            return label
    quoted = re.findall(r"«([^«»]+)»", name)
    text = quoted[-1] if quoted else name
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit and not quoted:
        named = re.search(r"\bимени\s+(.+)$", text)
        if named:
            text = f"Олимпиада им. {named.group(1)}"
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def button_name(o: CatalogOlympiad, limit: int = 50) -> str:
    return "ВсОШ" if o.is_vsosh else short_name(o.name, limit)


def sort_key(text: str) -> str:
    """Сортировка по алфавиту без учёта регистра, кавычек и «ё»."""
    return re.sub(r"[«»\"]", "", text).lower().replace("ё", "е")


def level_list(level: int, items: list, total: int, page: int, pages: int, first_number: int,
               current_year: str) -> str:
    """Список олимпиад уровня: items — [(олимпиада, профили этого уровня)] текущей страницы."""
    head = f"{LEVEL_ICONS[level]} <b>{level} уровень</b> — {_count(total)}"
    if pages > 1:
        head += f", страница {page + 1} из {pages}"
    lines = [head, ""]
    for number, (o, profiles) in enumerate(items, start=first_number):
        profiles_text = ", ".join(profiles)
        if len(profiles_text) > 150:
            profiles_text = profiles_text[:149].rstrip(", ") + "…"
        lines.append(f"<b>{number}. {escape(button_name(o, 70))}</b>\n    {escape(profiles_text)}")
    lines += ["", f"<i>Перечень {current_year}, по алфавиту. Под названием — профили {level} уровня.</i>"]
    return "\n".join(lines)


def level_text(level: int | None) -> str:
    return f"{level} ур." if level else "без уровня"


def short_year(academic_year: str) -> str:
    return academic_year[2:]  # 2026/27 -> 26/27


# ---------------------------------------------------------------------------
# Поиск
# ---------------------------------------------------------------------------

def hit_button_label(hit: SearchHit, current_year: str) -> str:
    """«Физтех · 1 ур.», «ВсОШ · физика · БВИ», «Робофест · не в перечне»."""
    o = hit.olympiad
    label = "ВсОШ" if o.is_vsosh else short_name(o.name, 40)
    if hit.matched_profiles and len(hit.matched_profiles) == 1:
        profile = hit.matched_profiles[0]
        label += f" · {profile if len(profile) <= 28 else profile[:27].rstrip() + '…'}"
    if o.is_vsosh:
        return label + " · БВИ"
    if current_year not in o.years:
        return label + " · не в перечне"
    level = o.best_level(current_year, set(hit.matched_profiles) or None)
    return f"{label} · {level_text(level)}" if level else label


def search_results(query: str, total: int, page: int, pages: int, current_year: str) -> str:
    if not total:
        return NOT_FOUND.format(query=escape(query))
    text = f"🔍 <b>{escape(query)}</b> — {_count(total)}"
    if pages > 1:
        text += f", страница {page + 1} из {pages}"
    return text + f"\n\n<i>Уровень — по перечню {current_year}. Выбери олимпиаду:</i>"


def _count(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        word = "олимпиада"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        word = "олимпиады"
    else:
        word = "олимпиад"
    return f"{n} {word}"


# ---------------------------------------------------------------------------
# Карточка олимпиады
# ---------------------------------------------------------------------------

def _subjects_line(profile_name: str, subjects: list[str], limit: int = 140) -> str | None:
    if not subjects or subjects == [profile_name]:
        return None
    text = ", ".join(subjects)
    return text if len(text) <= limit else text[: limit - 1].rstrip(", ") + "…"


def _by_year_desc(by_year: dict) -> list:
    return sorted(by_year.items(), key=lambda kv: year_start(kv[0]), reverse=True)


def _levels_by_group(profiles: dict) -> list[str]:
    """«1 уровень: математика, физика» — профили, сгруппированные по уровню."""
    groups: dict[int | None, list[str]] = {}
    for name, p in profiles.items():
        groups.setdefault(p.level, []).append(name)
    lines = []
    for level in sorted(groups, key=lambda lv: lv or 4):
        names = ", ".join(escape(n) for n in sorted(groups[level]))
        icon = LEVEL_ICONS.get(level, "▫️")
        label = f"{level} уровень" if level else "без уровня"
        lines.append(f"{icon} <b>{label}:</b> {names}")
    return lines


def _changes(o: CatalogOlympiad, current_year: str, prev_year: str) -> list[str]:
    """Что изменилось в перечне текущего года по сравнению с прошлым."""
    now, before = o.profiles_in(current_year), o.profiles_in(prev_year)
    lines = []
    for name in sorted(now):
        if name not in before:
            lines.append(f"🆕 {escape(name)} — новый профиль")
            continue
        old, new = before[name].level, now[name].level
        if old and new and old != new:
            arrow = "⬆️" if new < old else "⬇️"
            lines.append(f"{arrow} {escape(name)}: {old} → {new} уровень")
    for name in sorted(set(before) - set(now)):
        lines.append(f"✖️ {escape(name)} — убран из перечня")
    return lines


def _compact_table(o: CatalogOlympiad, current_year: str) -> list[str]:
    """Много профилей (НТО — почти 40 за 5 лет): строка на профиль, уровни по годам через точку."""
    years = sorted(o.years, key=year_start, reverse=True)
    lines = [
        "<b>Уровни по годам</b>",
        "<i>" + " · ".join(short_year(y) for y in years) + "  (— нет в перечне)</i>",
        "",
    ]
    current, former = [], []
    for name, by_year in o.profiles.items():
        levels = " · ".join(str(by_year[y].level or "?") if y in by_year else "—" for y in years)
        (current if current_year in by_year else former).append((name, levels, by_year))
    key = lambda item: ((item[2].get(current_year) or _by_year_desc(item[2])[0][1]).level or 4, item[0])  # noqa: E731
    lines += [f"<b>{escape(name)}</b>: {levels}" for name, levels, _ in sorted(current, key=key)]
    if former:
        lines += ["", "<b>Профили прошлых лет</b>"]
        lines += [f"{escape(name)}: {levels}" for name, levels, _ in sorted(former, key=key)]
    return lines


def olympiad_card(
    o: CatalogOlympiad, years: list[str], upcoming: list[Stage], today: date, detailed: bool = False
) -> str:
    """Карточка олимпиады. years — годы перечня в каталоге, от свежего к старому."""
    current_year = years[0]
    prev_year = years[1] if len(years) > 1 else None
    title = "ВсОШ" if o.is_vsosh else short_name(o.name, 60)
    lines = [f"<b>{escape(title)}</b>"]
    if title != o.name:
        lines.append(f"<i>{escape(o.name)}</i>")
    lines.append("")

    if o.is_vsosh:
        subjects = sorted(o.profiles_in(o.latest_year))
        lines += [
            "🏆 Победители и призёры заключительного этапа поступают <b>без вступительных испытаний</b> "
            "на направления, соответствующие профилю олимпиады.",
            "",
            f"<b>Предметы ({len(subjects)}):</b> " + escape(", ".join(subjects)),
        ]
    elif detailed:
        lines += _detailed_profiles(o, current_year)
    else:
        in_current = o.profiles_in(current_year)
        if in_current:
            lines.append(f"<b>Перечень {current_year}</b>")
            lines += _levels_by_group(in_current)
            changes = _changes(o, current_year, prev_year) if prev_year and prev_year in o.years else []
            if changes:
                lines += ["", f"<b>Изменения к {prev_year}</b>"] + changes
        else:
            last = o.latest_year
            lines += [
                f"⚠️ <b>Олимпиады нет в перечне {current_year}.</b>",
                "Дипломы прошлых лет действуют 4 года.",
                "",
                f"<b>Перечень {last}</b>",
            ] + _levels_by_group(o.profiles_in(last))

        dropped = [name for name, by_year in o.profiles.items()
                   if current_year not in by_year and (not prev_year or prev_year not in by_year)]
        if dropped and in_current:
            items = []
            for name in sorted(dropped)[:MAX_DROPPED]:
                year, p = _by_year_desc(o.profiles[name])[0]
                items.append(f"{escape(name)} ({p.level or '—'} ур. в {short_year(year)})")
            more = len(dropped) - MAX_DROPPED
            tail = f" и ещё {more} — в «Подробнее»" if more > 0 else ""
            lines += ["", "<b>Профили прошлых лет:</b> " + ", ".join(items) + tail]

    lines.append("")
    if upcoming:
        lines.append("📅 <b>Ближайшие этапы</b>")
        for s in upcoming:
            profile = f" ({escape(s.profile)})" if s.profile else ""
            lines.append(f"• {ru_period(s.starts_at, s.ends_at)} — {escape(s.title)}{profile}")
    else:
        lines.append("📅 <i>Даты этапов пока не загружены. Подпишись — напишу, когда появятся.</i>")

    return _fit(lines)


def _detailed_profiles(o: CatalogOlympiad, current_year: str) -> list[str]:
    """Каждый профиль: уровень, предметы, уровни по годам."""
    if len(o.profiles) > DETAIL_SUBJECTS_UP_TO:
        return _compact_table(o, current_year)
    lines = ["<b>Профили и уровни по годам</b>", ""]
    order = sorted(
        o.profiles,
        key=lambda n: (current_year not in o.profiles[n],
                       (o.profiles[n].get(current_year) or _by_year_desc(o.profiles[n])[0][1]).level or 4, n),
    )
    for name in order:
        by_year = o.profiles[name]
        latest_year, latest = _by_year_desc(by_year)[0]
        level = f"{latest.level} ур." if latest.level else "без уровня"
        status = "" if latest_year == current_year else f" · нет в перечне {current_year}"
        lines.append(f"<b>{escape(name)}</b> — {level}{status}")
        subjects = _subjects_line(name, latest.subjects)
        if subjects:
            lines.append(f"   предметы: {escape(subjects)}")
        if len(by_year) > 1 or latest_year != current_year:
            lines.append("   " + " · ".join(f"{short_year(y)}: {p.level or '—'}" for y, p in _by_year_desc(by_year)))
    return lines


def _host(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url).strip("/").split("/")[0] or url


def _fit(lines: list[str]) -> str:
    """Укладывает текст в лимит Telegram, обрезая с конца списка профилей."""
    text = "\n".join(lines)
    if len(text) <= MAX_MESSAGE:
        return text
    kept: list[str] = []
    for line in lines:
        if len("\n".join(kept + [line])) > MAX_MESSAGE - 80:
            break
        kept.append(line)
    hidden = len(lines) - len(kept)
    return "\n".join(kept + ["", f"<i>…и ещё {hidden} строк. Полный список — на сайте олимпиады.</i>"])


# ---------------------------------------------------------------------------
# Подписки и напоминания
# ---------------------------------------------------------------------------

def subscribed(o: CatalogOlympiad) -> str:
    return f"Готово! Напомню об этапах олимпиады «{escape(short_name(o.name, 80))}»."


def unsubscribed(o: CatalogOlympiad) -> str:
    return f"Подписка на «{escape(short_name(o.name, 80))}» отменена."


def subscriptions_list(olympiads: list[CatalogOlympiad]) -> str:
    if not olympiads:
        return ("🔔 <b>Подписки</b>\n\nПока пусто. Найди олимпиаду и нажми «Подписаться» в её карточке — "
                "я напомню о регистрации и этапах.")
    return (f"🔔 <b>Подписки ({len(olympiads)})</b>\n\n"
            "Напомню о регистрации и этапах этих олимпиад. Нажми на олимпиаду, чтобы открыть карточку.")


def reminder(r: Reminder, o: CatalogOlympiad) -> str:
    s = r.stage
    when = days_phrase(r.days_left)
    profile = f" ({escape(s.profile)})" if s.profile else ""
    name = escape(short_name(o.name, 80))
    if s.kind == "registration":
        head = f"⏰ Регистрация на «{name}»{profile} закрывается {when}"
        if r.days_left > 1:
            head += f", {ru_date(s.anchor_date)}"
        head += "."
    else:
        head = f"⏰ {when.capitalize()}: «{name}»{profile} — {escape(s.title)}, {ru_period(s.starts_at, s.ends_at)}."
    if s.source_url:
        head += f'\n\n<a href="{escape(s.source_url, quote=True)}">Подробнее на сайте олимпиады</a>'
    return head
