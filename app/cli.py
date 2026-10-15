"""Командная строка проекта.

Примеры:
    python -m app.cli db migrate
    python -m app.cli rsr fetch                      # текущий перечень
    python -m app.cli rsr fetch --years 2025 2024 2023 2022
    python -m app.cli rsr parse saved_page.html --year 2026/27
    python -m app.cli rsr load data/rsr/rsr_2026-27.json
    python -m app.cli rsr sql data/rsr/rsr_2026-27.json > load.sql
    python -m app.cli vsosh load --year 2026/27
    python -m app.cli bot run
    python -m app.cli stages import stages.csv
    python -m app.cli stages list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import settings
from app.exporters import load_json, save_csv, save_json, year_slug
from app.parsers.rsr import RsrList, parse_rsr_html, rsr_url_for_year


def _report(rsr: RsrList) -> None:
    print(f"  {rsr.academic_year}: олимпиад {len(rsr.olympiads)}, профилей {rsr.profile_count}")
    for warning in rsr.warnings:
        print(f"  ! {warning}")


def _save(rsr: RsrList, out_dir: Path, html: str | None = None) -> None:
    slug = year_slug(rsr.academic_year)
    if html is not None:
        raw = out_dir / "raw" / f"rsr_{slug}.html"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(html, encoding="utf-8")
    json_path = save_json(rsr, out_dir / f"rsr_{slug}.json")
    csv_path = save_csv(rsr, out_dir / f"rsr_{slug}.csv")
    print(f"  сохранено: {json_path}, {csv_path}")


def cmd_rsr_fetch(args: argparse.Namespace) -> int:
    from app.fetch import fetch_html  # requests нужен только для загрузки

    years: list[int | None] = args.years or [None]
    out_dir = Path(args.out)
    failed = 0
    for year in years:
        url = rsr_url_for_year(year)
        print(f"Загружаю {url}")
        try:
            html = fetch_html(url)
            rsr = parse_rsr_html(html, url=url)
        except Exception as exc:  # noqa: BLE001 — продолжаем с остальными годами
            print(f"  ошибка: {exc}", file=sys.stderr)
            failed += 1
            continue
        _report(rsr)
        _save(rsr, out_dir, html)
    return 1 if failed else 0


def cmd_rsr_parse(args: argparse.Namespace) -> int:
    html = Path(args.html).read_text(encoding="utf-8")
    rsr = parse_rsr_html(html, url=args.url, academic_year=args.year)
    _report(rsr)
    _save(rsr, Path(args.out))
    return 0


def cmd_rsr_sql(args: argparse.Namespace) -> int:
    from app.db.sql_loader import rsr_to_sql

    sys.stdout.write(rsr_to_sql(load_json(Path(args.json))))
    return 0


def cmd_rsr_load(args: argparse.Namespace) -> int:
    from app.db.psql import run_psql
    from app.db.sql_loader import rsr_to_sql

    for path in args.json:
        rsr = load_json(Path(path))
        run_psql(rsr_to_sql(rsr), database_url=args.database_url)
        print(f"Загружено в базу: {rsr.academic_year} ({len(rsr.olympiads)} олимпиад, {rsr.profile_count} профилей)")
    return 0


def cmd_vsosh_load(args: argparse.Namespace) -> int:
    from app.db.psql import run_psql
    from app.db.sql_loader import rsr_to_sql
    from app.parsers.vsosh import vsosh_list

    vsosh = vsosh_list(args.year)
    run_psql(rsr_to_sql(vsosh, source="vsosh", is_vsosh=True), database_url=args.database_url)
    print(f"Загружена ВсОШ на {args.year}: {vsosh.profile_count} предметов")
    return 0


def _catalog():
    from app.catalog import Catalog

    try:
        return Catalog.load(settings.data_dir / "rsr")
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc


def cmd_bot_run(args: argparse.Namespace) -> int:
    import asyncio
    import logging

    token = settings.telegram_bot_token
    if not token:
        print("Не задан TELEGRAM_BOT_TOKEN. Добавьте его в файл .env (см. .env.example)", file=sys.stderr)
        return 1
    try:
        from app.bot.main import run
    except ImportError as exc:
        print(f"Не установлены зависимости бота ({exc}). Выполните: pip install -r requirements.txt", file=sys.stderr)
        return 1
    from app.bot.storage import Storage

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    catalog = _catalog()
    storage = Storage(settings.bot_db_path)
    print(f"Каталог: {len(catalog.olympiads)} олимпиад, годы {', '.join(catalog.years)}. База бота: {settings.bot_db_path}")
    print("Бот запущен, остановка — Ctrl+C")
    try:
        asyncio.run(run(token, catalog, storage))
    except KeyboardInterrupt:
        pass
    finally:
        storage.close()
    return 0


def cmd_stages_import(args: argparse.Namespace) -> int:
    from app.bot.storage import Storage
    from app.stages import read_stages_csv

    catalog = _catalog()

    def resolve(text: str):
        found = catalog.find_one(text)
        if found:
            return found.id
        hits = catalog.search(text, limit=2)
        return hits[0].olympiad.id if len(hits) == 1 else None

    try:
        rows = read_stages_csv(Path(args.csv), resolve, default_year=catalog.current_year)
    except (ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    storage = Storage(settings.bot_db_path)
    count = storage.upsert_stages(rows)
    storage.close()
    print(f"Загружено этапов: {count}")
    return 0


def cmd_stages_list(args: argparse.Namespace) -> int:
    from app.bot.storage import Storage

    catalog = _catalog()
    storage = Storage(settings.bot_db_path)
    stages = storage.stages()
    storage.close()
    if not stages:
        print("Этапов нет. Загрузите их: python -m app.cli stages import файл.csv")
    for s in stages:
        o = catalog.get(s.olympiad_id)
        name = o.name if o else s.olympiad_id
        dates = " – ".join(d.isoformat() for d in (s.starts_at, s.ends_at) if d)
        print(f"{dates:25} {s.kind:13} {name} — {s.title}" + (f" ({s.profile})" if s.profile else ""))
    return 0


def cmd_db_migrate(args: argparse.Namespace) -> int:
    from app.db.psql import migrate

    applied = migrate(args.database_url)
    print("Применены миграции: " + ", ".join(applied) if applied else "Новых миграций нет")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Навигатор по олимпиадам")
    groups = parser.add_subparsers(dest="group", required=True)

    db = groups.add_parser("db", help="база данных").add_subparsers(dest="command", required=True)
    p = db.add_parser("migrate", help="применить новые миграции")
    p.add_argument("--database-url", default=None, help="по умолчанию DATABASE_URL")
    p.set_defaults(func=cmd_db_migrate)

    rsr = groups.add_parser("rsr", help="перечень РСОШ").add_subparsers(dest="command", required=True)

    p = rsr.add_parser("fetch", help="скачать и разобрать перечень с rsr-olymp.ru")
    p.add_argument("--years", type=int, nargs="*", help="первые годы учебных лет из архива, например 2025 2024")
    p.add_argument("--out", default=str(settings.data_dir / "rsr"))
    p.set_defaults(func=cmd_rsr_fetch)

    p = rsr.add_parser("parse", help="разобрать сохранённую страницу")
    p.add_argument("html")
    p.add_argument("--year", help="учебный год, например 2026/27 (иначе определяется по странице)")
    p.add_argument("--url", help="адрес, с которого сохранена страница")
    p.add_argument("--out", default=str(settings.data_dir / "rsr"))
    p.set_defaults(func=cmd_rsr_parse)

    p = rsr.add_parser("sql", help="вывести SQL загрузки JSON в базу")
    p.add_argument("json")
    p.set_defaults(func=cmd_rsr_sql)

    p = rsr.add_parser("load", help="загрузить JSON перечня в базу")
    p.add_argument("json", nargs="+")
    p.add_argument("--database-url", default=None)
    p.set_defaults(func=cmd_rsr_load)

    vsosh = groups.add_parser("vsosh", help="Всероссийская олимпиада школьников").add_subparsers(
        dest="command", required=True)
    p = vsosh.add_parser("load", help="загрузить предметы ВсОШ в базу")
    p.add_argument("--year", default="2026/27")
    p.add_argument("--database-url", default=None)
    p.set_defaults(func=cmd_vsosh_load)

    bot = groups.add_parser("bot", help="Telegram-бот").add_subparsers(dest="command", required=True)
    p = bot.add_parser("run", help="запустить бота")
    p.set_defaults(func=cmd_bot_run)

    stages = groups.add_parser("stages", help="этапы олимпиад").add_subparsers(dest="command", required=True)
    p = stages.add_parser("import", help="загрузить этапы из CSV")
    p.add_argument("csv")
    p.set_defaults(func=cmd_stages_import)
    p = stages.add_parser("list", help="показать загруженные этапы")
    p.set_defaults(func=cmd_stages_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
