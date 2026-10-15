"""Тексты сообщений бота (HTML-разметка Telegram). Без зависимостей от aiogram — легко тестировать."""

from __future__ import annotations

import re
from datetime import date
from html import escape

from app.benefits import BENEFIT_TYPES, DIPLOMAS, Rule, UniversityBenefits
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


def stage_when(s: Stage) -> str:
    """«до 31 октября» для дедлайнов, иначе дата или период."""
    if s.starts_at is None and s.ends_at is not None:
        return f"до {ru_date(s.ends_at)}"
    return ru_period(s.starts_at, s.ends_at)


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


FILLER = "\u2800"  # пустой символ Брайля: Telegram его не обрезает, в отличие от пробелов
LEFT_ALIGN_WIDTH = 34  # примерно ширина кнопки на телефоне в символах


def left_align(label: str, width: int = LEFT_ALIGN_WIDTH) -> str:
    """Прижимает подпись кнопки к левому краю.

    Telegram всегда центрирует текст в кнопке. Дополняем короткую подпись справа
    невидимыми символами до ширины кнопки на телефоне — текст визуально уходит влево.
    Длинные подписи не трогаем, иначе Telegram обрежет их многоточием.
    """
    return label + FILLER * (width - len(label)) if len(label) < width else label


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
            lines.append(f"• {stage_when(s)} — {escape(s.title)}{profile}")
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


MAX_ANNOUNCED = 8


def new_stages(o: CatalogOlympiad, stages: list[Stage]) -> str:
    """Сообщение подписчику: у олимпиады появились даты."""
    name = escape("ВсОШ" if o.is_vsosh else short_name(o.name, 80))
    ordered = sorted(stages, key=lambda s: (s.starts_at or s.ends_at, s.title))
    lines = [f"📅 <b>Появились даты: «{name}»</b>", ""]
    for s in ordered[:MAX_ANNOUNCED]:
        profile = f" ({escape(s.profile)})" if s.profile else ""
        lines.append(f"• {stage_when(s)} — {escape(s.title)}{profile}")
    if len(ordered) > MAX_ANNOUNCED:
        lines.append(f"…и ещё {len(ordered) - MAX_ANNOUNCED} — в карточке олимпиады.")
    lines += ["", "Напомню ближе к делу."]
    return "\n".join(lines)


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


# ---------------------------------------------------------------------------
# Льготы вузов
# ---------------------------------------------------------------------------

BENEFIT_ICONS = {"bvi": "🏅", "score100": "💯"}


def _rule_line(r: Rule) -> str:
    """«🏅 БВИ — победителям (математика, физика; 1–2 ур.)»"""
    details = []
    if r.profiles:
        details.append(", ".join(r.profiles))
    if r.levels and r.levels != [1, 2, 3]:
        details.append("–".join(str(x) for x in (min(r.levels), max(r.levels))).replace(
            f"{min(r.levels)}–{min(r.levels)}", str(min(r.levels))) + " ур.")
    line = f"{BENEFIT_ICONS[r.benefit]} {BENEFIT_TYPES[r.benefit]} — {DIPLOMAS[r.diploma]}"
    if details:
        line += f" ({escape('; '.join(details))})"
    if r.programs:
        programs = ", ".join(r.programs)
        line += f"\n      направления: {escape(programs if len(programs) <= 160 else programs[:159] + '…')}"
    return line


def _conditions(r: Rule) -> str | None:
    parts = []
    if r.confirm_min_score:
        subject = f" по {r.confirm_subject}" if r.confirm_subject else " по предмету профиля"
        parts.append(f"ЕГЭ от {r.confirm_min_score}{subject}")
    elif r.olympiad.is_vsosh:
        parts.append("без условия по ЕГЭ")
    if r.grades:
        parts.append("диплом за " + " или ".join(str(g) for g in r.grades) + " класс")
    if r.diploma_since:
        parts.append(f"не раньше {r.diploma_since} года")
    return " · ".join(parts) or None


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} {few}"
    return f"{n} {many}"


def _summary(rules: list[Rule]) -> list[str]:
    """Сводка правил одного вуза: «🏅 БВИ — победителям и призёрам: математика (10 программ), физика (2)»."""
    groups: dict[tuple[str, str], dict[str, set[str] | None]] = {}
    for r in rules:
        by_profile = groups.setdefault((r.benefit, r.diploma), {})
        for profile in r.profiles or ["любой профиль"]:
            if r.programs is None:
                by_profile[profile] = None
            elif by_profile.get(profile, set()) is not None:
                by_profile.setdefault(profile, set()).update(r.programs)
    lines = []
    for (benefit, diploma) in sorted(groups, key=lambda k: (k[0] != "bvi", k[1] != "any")):
        parts = []
        for profile, programs in sorted(groups[(benefit, diploma)].items()):
            label = escape(profile)
            if programs:
                label += f" ({_plural(len(programs), 'программа', 'программы', 'программ')})" if len(programs) > 1 \
                    else f" ({escape(next(iter(programs)))})"
            parts.append(label)
        lines.append(f"{BENEFIT_ICONS[benefit]} {BENEFIT_TYPES[benefit]} — {DIPLOMAS[diploma]}: " + "; ".join(parts))
    scores = sorted({r.confirm_min_score for r in rules if r.confirm_min_score})
    conditions = []
    if scores:
        conditions.append(f"ЕГЭ от {scores[0]}" if len(scores) == 1 else f"ЕГЭ от {scores[0]} до {scores[-1]} — смотря по программе")
    elif any(r.olympiad.is_vsosh for r in rules):
        conditions.append("без условия по ЕГЭ")
    grades = {g for r in rules for g in (r.grades or [])}
    if grades == {11}:
        conditions.append("диплом за 11 класс")
    elif 10 in grades:
        conditions.append("часть льгот — и за 10 класс")
    since = {r.diploma_since for r in rules if r.diploma_since}
    if since:
        conditions.append(f"диплом не раньше {min(since)} года")
    if conditions:
        lines.append(f"      <i>{escape(' · '.join(conditions))}</i>")
    return lines


def olympiad_benefits(o: CatalogOlympiad, entries: list[tuple[UniversityBenefits, list[Rule]]],
                      admission_year: int) -> str:
    """Сводка льгот олимпиады по всем вузам."""
    name = escape("ВсОШ" if o.is_vsosh else short_name(o.name, 60))
    if not entries:
        return (f"🎓 <b>Льготы: «{name}»</b>\n\nПока нет данных ни по одному вузу из тех, что мы собрали. "
                f"Список вузов пополняется — загляни в «🎓 Вузы» в меню.")
    lines = [f"🎓 <b>Льготы: «{name}»</b>", ""]
    for u, rules in entries:
        lines.append(f"<b>{escape(u.university)}</b> — <i>{u.status_label(admission_year)}</i>")
        lines += _summary(rules)
        if u.is_draft and u.notes:
            lines.append(f"⚠️ <i>{escape(u.notes[0])}</i>")
        lines.append("")
    lines.append("<i>Программы и точные условия — кнопкой вуза ниже. Всегда сверяй с правилами приёма вуза.</i>")
    return _fit(lines)


def olympiad_university_benefits(o: CatalogOlympiad, u: UniversityBenefits, rules: list[Rule],
                                 admission_year: int) -> str:
    """Все правила одного вуза для олимпиады — со списками программ."""
    name = escape("ВсОШ" if o.is_vsosh else short_name(o.name, 60))
    lines = [f"🎓 <b>{escape(u.university)}: «{name}»</b>", f"<i>{u.status_label(admission_year)}</i>", ""]
    order = sorted(rules, key=lambda r: (r.benefit != "bvi", (r.profiles or [""])[0], r.diploma != "any"))
    for r in order:
        lines.append(_rule_line(r).split("\n")[0])
        extra = [c for c in (_conditions(r), r.note) if c]
        if extra:
            lines.append(f"      <i>{escape(' · '.join(extra))}</i>")
        if r.programs:
            lines.append("      " + escape("; ".join(r.programs)))
        elif r.programs is None and not r.olympiad.is_vsosh:
            lines.append("      <i>направления — по профилю олимпиады</i>")
    lines += ["", f'Источник: <a href="{escape(u.source_url, quote=True)}">{escape(u.source_title or u.source_url)}</a>']
    return _fit(lines)


def universities_list(universities: list[UniversityBenefits], admission_year: int) -> str:
    if not universities:
        return "🎓 <b>Вузы</b>\n\nДанные о льготах пока не загружены."
    lines = [f"🎓 <b>Вузы — {len(universities)}</b>", "",
             "Какие олимпиады дают БВИ и 100 баллов. Выбери вуз:", ""]
    for u in universities:
        lines.append(f"• <b>{escape(u.university)}</b> — {u.status_label(admission_year)}")
    return "\n".join(lines)


def university_benefits(u: UniversityBenefits, admission_year: int) -> str:
    lines = [f"🎓 <b>{escape(u.university)}</b>", f"<i>{escape(u.full_name)}</i>", "",
             f"📄 {u.status_label(admission_year).capitalize()}"]
    if u.admission_year < admission_year:
        lines.append(f"⚠️ Правила на приём {admission_year} вуз ещё не опубликовал — это данные прошлого года.")
    for n in u.notes:
        lines.append(f"• {escape(n)}")
    lines.append("")
    groups: dict[tuple[str, str], list[str]] = {}
    for r in u.rules:
        label = button_name(r.olympiad, 80)
        if r.profiles:
            label += f" ({', '.join(r.profiles)})"
        groups.setdefault((r.benefit, r.diploma), [])
        if label not in groups[(r.benefit, r.diploma)]:
            groups[(r.benefit, r.diploma)].append(label)
    order = sorted(groups, key=lambda k: (k[0] != "bvi", k[1]))
    for benefit, diploma in order:
        names = sorted(groups[(benefit, diploma)], key=sort_key)
        lines.append(f"{BENEFIT_ICONS[benefit]} <b>{BENEFIT_TYPES[benefit]} — {DIPLOMAS[diploma]}</b> ({len(names)}):")
        lines.append(escape(", ".join(names)))
        lines.append("")
    lines.append(f'Источник: <a href="{escape(u.source_url, quote=True)}">{escape(u.source_title or u.source_url)}</a>, '
                 f"проверено {ru_date(u.checked_at)} {u.checked_at.year}")
    return _fit(lines)
