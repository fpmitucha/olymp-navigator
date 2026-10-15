"""Генерация идемпотентного SQL для загрузки перечня в PostgreSQL.

Почему SQL, а не ORM: загрузка перечня — пакетная операция раз в год, а SQL-файл
легко проверить глазами и применить через psql на любом сервере.

Повторный запуск с тем же годом безопасен: строки перечня этого года из
источника 'rsr' заменяются целиком, олимпиады обновляются по ключу.
Льготы ссылаются на олимпиаду и название профиля, поэтому их перезагрузка не трогает.
"""

from __future__ import annotations

from app.parsers.rsr import RsrList


def sql_literal(value) -> str:
    """Безопасная запись значения в SQL-литерал (standard_conforming_strings = on)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return "'{}'::text[]"
        return "ARRAY[" + ", ".join(sql_literal(v) for v in value) + "]::text[]"
    text = str(value)
    if "\x00" in text:
        raise ValueError("Нулевой байт в строке недопустим в PostgreSQL")
    return "'" + text.replace("'", "''") + "'"


def rsr_to_sql(rsr: RsrList, *, source: str = "rsr", is_vsosh: bool = False) -> str:
    """Возвращает SQL-скрипт загрузки одного года перечня. Скрипт выполняется в одной транзакции."""
    year = rsr.academic_year
    lines: list[str] = [
        f"-- Загрузка олимпиад (источник: {source}) на {year} учебный год",
        f"-- Источник: {rsr.source_url or 'не указан'}; разобрано: {rsr.parsed_at}",
        f"-- Олимпиад: {len(rsr.olympiads)}, профилей: {rsr.profile_count}",
        "BEGIN;",
        "",
    ]

    for o in rsr.olympiads:
        lines.append(
            "INSERT INTO olympiad (key, name, name_year, is_vsosh) VALUES "
            f"({sql_literal(o.key)}, {sql_literal(o.name)}, {sql_literal(year)}, {sql_literal(is_vsosh)})\n"
            "  ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, name_year = EXCLUDED.name_year,\n"
            "    is_vsosh = olympiad.is_vsosh OR EXCLUDED.is_vsosh, updated_at = now()\n"
            "  WHERE olympiad.name_year <= EXCLUDED.name_year;"
        )

    lines += [
        "",
        f"DELETE FROM olympiad_profile WHERE academic_year = {sql_literal(year)} AND source = {sql_literal(source)};",
        "",
    ]

    for o in rsr.olympiads:
        for p in o.profiles:
            values = ", ".join([
                "id",
                sql_literal(year),
                sql_literal(source),
                sql_literal(o.number),
                sql_literal(o.name),
                sql_literal(p.profile),
                sql_literal(p.subjects_raw),
                sql_literal(p.subjects),
                sql_literal(p.level),
                sql_literal(p.website),
                sql_literal(rsr.source_url),
            ])
            lines.append(
                "INSERT INTO olympiad_profile (olympiad_id, academic_year, source, list_number, name_in_list, "
                "profile, subjects_raw, subjects, level, website, source_url)\n"
                f"  SELECT {values} FROM olympiad WHERE key = {sql_literal(o.key)};"
            )

    lines += ["", "COMMIT;", ""]
    return "\n".join(lines)
