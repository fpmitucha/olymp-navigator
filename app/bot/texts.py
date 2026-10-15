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
    "Привет! Я помогаю разобраться с олимпиадами из перечня РСОШ и ВсОШ.\n\n"
    "Напиши название олимпиады, сокращение или предмет, например:\n"
    "• <code>Физтех</code>\n"
    "• <code>ВсОШ</code>\n"
    "• <code>информатика</code>\n"
    "• <code>робототехника</code>\n\n"
    "Я покажу профили и уровни олимпиады за последние годы. Подпишись — и я напомню "
    "о регистрации и этапах."
)

HELP = (
    "<b>Как пользоваться</b>\n\n"
    "Отправь название олимпиады, сокращение (ВсОШ, МОШ, НТО, ВШЭ, МГУ, СПбГУ) или предмет. "
    "В карточке олимпиады — профили и их уровни.\n\n"
    "<b>Почему важны уровни прошлых лет.</b> Диплом действует 4 года, а вуз учитывает уровень олимпиады "
    "в том году, когда диплом получен.\n\n"
    "<b>Подписки.</b> Кнопка «Подписаться» в карточке. Я напомню о регистрации за 3 дня и в последний день, "
    "об этапах — за неделю, за день и в день этапа.\n\n"
    "/subs — мои подписки\n"
    "/help — эта справка\n\n"
    "Данные берутся с сайта Российского совета олимпиад школьников. Окончательную информацию "
    "всегда сверяй на сайте олимпиады и вуза."
)

NOT_FOUND = (
    "Ничего не нашлось по запросу «{query}».\n\n"
    "Попробуй короче: одно слово из названия, сокращение (МОШ, НТО, ВШЭ) или предмет."
)


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


def level_text(level: int | None) -> str:
    return f"{level} ур." if level else "без уровня"


def short_year(academic_year: str) -> str:
    return academic_year[2:]  # 2026/27 -> 26/27


# ---------------------------------------------------------------------------
# Поиск
# ---------------------------------------------------------------------------

def hit_button_label(hit: SearchHit, current_year: str) -> str:
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


def search_results(query: str, hits: list[SearchHit], total_limit_reached: bool) -> str:
    if not hits:
        return NOT_FOUND.format(query=escape(query))
    text = f"По запросу «{escape(query)}» нашлось: {len(hits)}. Выбери олимпиаду:"
    if total_limit_reached:
        text += "\n\n<i>Показаны первые результаты, уточни запрос, чтобы увидеть остальные.</i>"
    return text


# ---------------------------------------------------------------------------
# Карточка олимпиады
# ---------------------------------------------------------------------------

def _subjects_line(profile_name: str, subjects: list[str]) -> str | None:
    if not subjects or subjects == [profile_name]:
        return None
    text = ", ".join(subjects)
    if len(text) > 160:
        text = text[:159].rstrip(", ") + "…"
    return text


def _history(o: CatalogOlympiad, profile_name: str, current_year: str) -> str | None:
    by_year = o.profiles[profile_name]
    current = by_year.get(current_year)
    previous = [(y, p.level) for y, p in sorted(by_year.items(), key=lambda kv: year_start(kv[0]), reverse=True)
                if y != current_year]
    if not previous or all(level == (current.level if current else None) for _, level in previous):
        return None
    return ", ".join(f"{short_year(y)} — {level or '—'}" for y, level in previous)


def olympiad_card(o: CatalogOlympiad, current_year: str, upcoming: list[Stage], today: date) -> str:
    lines = [f"<b>{escape(o.name)}</b>", ""]

    if o.is_vsosh:
        lines += [
            "Всероссийская олимпиада школьников. Победители и призёры заключительного этапа "
            "имеют право на поступление без вступительных испытаний (БВИ) по профилю олимпиады.",
            "",
            f"<b>Предметы ({len(o.profiles_in(o.latest_year))}):</b> "
            + escape(", ".join(sorted(o.profiles_in(o.latest_year)))),
        ]
    else:
        in_current = o.profiles_in(current_year)
        if in_current:
            show_subjects = len(in_current) <= 10  # у НТО 20 профилей: без предметов карточка влезает
            lines.append(f"<b>Перечень {current_year}:</b>")
            for name in sorted(in_current, key=lambda n: (in_current[n].level or 4, n)):
                p = in_current[name]
                lines.append(f"• {escape(name)} — <b>{level_text(p.level)}</b>")
                subjects = _subjects_line(name, p.subjects) if show_subjects else None
                if subjects:
                    lines.append(f"   <i>{escape(subjects)}</i>")
                history = _history(o, name, current_year)
                if history:
                    lines.append(f"   прошлые годы: {history}")
        else:
            lines.append(
                f"⚠️ <b>Олимпиады нет в перечне {current_year}.</b> Дипломы прошлых лет действуют 4 года."
            )

        dropped = {
            name: by_year for name, by_year in o.profiles.items()
            if current_year not in by_year
        }
        if dropped:
            lines += ["", "<b>Профили прошлых лет</b> (дипломы ещё действуют):"]
            for name in sorted(dropped):
                history = ", ".join(
                    f"{short_year(y)} — {p.level or '—'}"
                    for y, p in sorted(dropped[name].items(), key=lambda kv: year_start(kv[0]), reverse=True)
                )
                lines.append(f"• {escape(name)}: {history}")

    if upcoming:
        lines += ["", "<b>Ближайшие этапы:</b>"]
        for s in upcoming:
            profile = f" ({escape(s.profile)})" if s.profile else ""
            lines.append(f"• {ru_period(s.starts_at, s.ends_at)} — {escape(s.title)}{profile}")
    else:
        lines += ["", "<i>Даты этапов пока не загружены. Подпишись — я напишу, когда они появятся.</i>"]

    if o.websites:
        links = ", ".join(f'<a href="{escape(url, quote=True)}">{escape(_host(url))}</a>' for url in o.websites[:2])
        lines += ["", f"Сайт: {links}"]

    return _fit(lines)


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
        return "У тебя пока нет подписок. Найди олимпиаду и нажми «Подписаться» в её карточке."
    lines = [f"<b>Твои подписки ({len(olympiads)}):</b>", ""]
    lines += [f"• {escape(short_name(o.name, 80))}" for o in olympiads]
    lines += ["", "Нажми на олимпиаду, чтобы открыть карточку или отписаться."]
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
