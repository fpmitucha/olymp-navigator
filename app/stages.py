"""Этапы олимпиад: модель, расчёт напоминаний и импорт из CSV.

Пока даты этапов не подтягиваются автоматически, их можно загрузить из CSV-файла
(формат описан в README). Следующим шагом сюда добавится загрузка из календаря olimpiada.ru.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

STAGE_KINDS: dict[str, str] = {
    "registration": "регистрация",
    "qualifying": "отборочный этап",
    "final": "заключительный этап",
    "results": "результаты",
    "other": "событие",
}

# Сколько дней до события присылать напоминание. Для регистрации важен её конец
# (дедлайн), для остальных этапов — начало.
REMINDER_WINDOWS: dict[str, list[tuple[str, int]]] = {
    # (вид напоминания, напоминаем, когда до даты осталось не больше N дней)
    "registration": [("deadline_t3", 3), ("deadline_day", 0)],
    "default": [("stage_t7", 7), ("stage_t1", 1), ("stage_day", 0)],
}


@dataclass(frozen=True)
class Stage:
    id: int
    olympiad_id: str
    kind: str
    title: str
    starts_at: date | None
    ends_at: date | None
    academic_year: str = ""
    profile: str | None = None
    source_url: str | None = None

    @property
    def kind_label(self) -> str:
        return STAGE_KINDS.get(self.kind, self.kind)

    @property
    def anchor_date(self) -> date | None:
        """Дата, к которой привязаны напоминания."""
        if self.kind == "registration":
            return self.ends_at or self.starts_at
        return self.starts_at or self.ends_at

    def is_upcoming(self, today: date) -> bool:
        last = self.ends_at or self.starts_at
        return last is not None and last >= today


@dataclass(frozen=True)
class Reminder:
    telegram_id: int
    stage: Stage
    kind: str
    days_left: int


def reminder_kind(stage: Stage, today: date) -> tuple[str, int] | None:
    """Какое напоминание положено по этапу сегодня: (вид, дней осталось) или None.

    Окна идут от дальнего к ближнему. Вид выбирается по самому узкому окну, в которое
    попадает дата, поэтому после пропуска дня (бот был выключен) напоминание всё равно
    придёт, а после «завтра» уже не придёт «через неделю».
    """
    anchor = stage.anchor_date
    if anchor is None:
        return None
    days_left = (anchor - today).days
    if days_left < 0:
        return None
    windows = REMINDER_WINDOWS["registration" if stage.kind == "registration" else "default"]
    chosen = None
    for kind, max_days in windows:
        if days_left <= max_days:
            chosen = kind
    return (chosen, days_left) if chosen else None


def due_reminders(
    today: date,
    stages: list[Stage],
    subscribers: dict[str, list[int]],
    already_sent: set[tuple[int, int, str]],
) -> list[Reminder]:
    """Напоминания, которые нужно отправить сегодня.

    subscribers: id олимпиады -> Telegram ID подписчиков.
    already_sent: множество (telegram_id, stage_id, вид) из журнала отправок.
    """
    result: list[Reminder] = []
    for stage in stages:
        due = reminder_kind(stage, today)
        if due is None:
            continue
        kind, days_left = due
        for telegram_id in subscribers.get(stage.olympiad_id, []):
            if (telegram_id, stage.id, kind) in already_sent:
                continue
            result.append(Reminder(telegram_id, stage, kind, days_left))
    return result


# ---------------------------------------------------------------------------
# Импорт из CSV
# ---------------------------------------------------------------------------

CSV_COLUMNS = ["olympiad", "kind", "title", "starts_at", "ends_at", "profile", "academic_year", "source_url"]


@dataclass
class StageRow:
    olympiad_id: str
    kind: str
    title: str
    starts_at: date | None
    ends_at: date | None
    profile: str | None
    academic_year: str
    source_url: str | None


def _parse_date(value: str, line: int, column: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    for fmt_value in (value, _ru_to_iso(value)):
        try:
            return date.fromisoformat(fmt_value)
        except (ValueError, TypeError):
            continue
    raise ValueError(f"Строка {line}: дата «{value}» в колонке {column} должна быть ГГГГ-ММ-ДД или ДД.ММ.ГГГГ")


def _ru_to_iso(value: str) -> str | None:
    parts = value.split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        d, m, y = parts
        return f"{y}-{int(m):02d}-{int(d):02d}"
    return None


def read_stages_csv(path: Path, resolve_olympiad, default_year: str) -> list[StageRow]:
    """Читает CSV с этапами. resolve_olympiad(text) -> id олимпиады или None.

    Разделитель — точка с запятой или запятая (определяется автоматически),
    кодировка UTF-8 (в том числе с BOM, как сохраняет Excel).
    """
    text = Path(path).read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,")
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    missing = {"olympiad", "kind", "starts_at"} - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"В CSV нет колонок: {', '.join(sorted(missing))}. Нужны: {', '.join(CSV_COLUMNS)}")

    rows: list[StageRow] = []
    errors: list[str] = []
    for line, raw in enumerate(reader, start=2):
        get = lambda name: (raw.get(name) or "").strip()  # noqa: E731
        if not any(get(c) for c in CSV_COLUMNS):
            continue
        try:
            olympiad_id = resolve_olympiad(get("olympiad"))
            if olympiad_id is None:
                raise ValueError(f"Строка {line}: олимпиада «{get('olympiad')}» не найдена в перечне")
            kind = get("kind")
            if kind not in STAGE_KINDS:
                raise ValueError(f"Строка {line}: вид этапа «{kind}», допустимы: {', '.join(STAGE_KINDS)}")
            starts_at = _parse_date(get("starts_at"), line, "starts_at")
            ends_at = _parse_date(get("ends_at"), line, "ends_at")
            if starts_at and ends_at and ends_at < starts_at:
                raise ValueError(f"Строка {line}: ends_at раньше starts_at")
            if not starts_at and not ends_at:
                raise ValueError(f"Строка {line}: нужна хотя бы одна дата")
            rows.append(StageRow(
                olympiad_id=olympiad_id,
                kind=kind,
                title=get("title") or STAGE_KINDS[kind],
                starts_at=starts_at,
                ends_at=ends_at,
                profile=get("profile") or None,
                academic_year=get("academic_year") or default_year,
                source_url=get("source_url") or None,
            ))
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("Ошибки в CSV:\n" + "\n".join(errors))
    return rows
