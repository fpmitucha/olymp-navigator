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
    python -m app.cli bot check
    python -m app.cli stages import stages.csv
    python -m app.cli stages sync
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


def _benefits(catalog, strict: bool = True):
    from app.benefits import BenefitsCatalog, BenefitsError, load_benefits
    from app.config import PROJECT_ROOT

    try:
        return load_benefits(PROJECT_ROOT / "benefits", catalog)
    except BenefitsError as exc:
        if strict:
            raise SystemExit(f"Ошибки в файлах льгот:\n{exc}") from exc
        print(f"⚠️ Льготы не загружены, ошибки в файлах benefits/:\n{exc}", file=sys.stderr)
        return BenefitsCatalog()


def cmd_benefits_check(args: argparse.Namespace) -> int:
    catalog = _catalog()
    benefits = _benefits(catalog)
    for u in benefits.universities:
        from collections import Counter

        from app.benefits import BENEFIT_TYPES

        counts = Counter(r.benefit for r in u.rules)
        kinds = ", ".join(f"{BENEFIT_TYPES[k]} {counts[k]}" for k in BENEFIT_TYPES if counts[k])
        print(f"✓ {u.file}: {u.university}, приём {u.admission_year}, {u.document_status}, "
              f"правил {len(u.rules)} ({kinds})")
    return 0


def cmd_benefits_import_hse(args: argparse.Namespace) -> int:
    import json
    import re
    from datetime import date

    from app.benefits import parse_file, resolve_import
    from app.parsers.hse_benefits import parse_hse_table

    source = args.source
    if source.startswith("http"):
        from app.fetch import fetch_bytes

        data = fetch_bytes(source)
    else:
        data = Path(source).read_bytes()
    table = parse_hse_table(data)
    catalog = _catalog()
    rules, problems = resolve_import(table.rules, catalog)
    year = int(m.group(1)) if (m := re.search(r"в (20\d\d) году", table.title)) else args.year
    doc = {
        "university": args.name,
        "full_name": "Национальный исследовательский университет «Высшая школа экономики»"
                     + (f" — {args.campus}" if args.campus and args.campus != "Москва" else ""),
        "city": args.campus or "Москва",
        "admission_year": year,
        "document_status": "approved",
        "source_url": args.page,
        "source_title": table.title or "Особые права при приёме в НИУ ВШЭ",
        "checked_at": date.today().isoformat(),
        "notes": [
            "Льгота действует, если в год написания олимпиада и её профиль входили в перечень Минобрнауки.",
            "Минимальный балл ЕГЭ для подтверждения — не ниже указанного в правиле (обычно 75).",
        ] + ([f"Ещё {table.skipped_generic} правил «для всех олимпиад перечня» (гуманитарные программы) "
              "пока не показаны — смотрите таблицу ВШЭ."] if table.skipped_generic else []),
        "defaults": {},
        "rules": rules,
    }
    parse_file(doc, catalog, args.out)  # та же проверка, что при загрузке бота
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{table.title}\nстрок таблицы: {table.rows}, программ: {table.programs}, правил: {len(table.rules)}, "
          f"сохранено: {len(rules)}, пропущено общих: {table.skipped_generic}")
    print(f"Записано: {args.out}")
    if problems:
        print(f"\nНе сопоставлено ({len(problems)}) — эти правила не сохранены:")
        for p in problems:
            print(f"  ? {p}")
    return 0


def cmd_benefits_import_msu(args: argparse.Namespace) -> int:
    import json
    import re
    from datetime import date

    from app.benefits import parse_file, resolve_import
    from app.parsers.msu_benefits import parse_msu_pdf

    table = parse_msu_pdf(args.source)
    catalog = _catalog()
    rsr_year = m.group(1) if (m := re.search(r"на (20\d\d/\d\d) учебный год", table.title)) else args.rsr_year
    rules, problems = resolve_import([{**r, "rsr_year": rsr_year} if r["olympiad"] == "*" else r
                                      for r in table.rules], catalog)
    year = int(m.group(1)) if (m := re.search(r"в (20\d\d) году", table.title)) else args.year
    doc = {
        "university": "МГУ",
        "full_name": "Московский государственный университет имени М.В. Ломоносова",
        "city": "Москва",
        "admission_year": year,
        "document_status": "approved",
        "source_url": args.url,
        "source_title": table.title or "Особые права победителям и призёрам олимпиад, МГУ",
        "checked_at": date.today().isoformat(),
        "notes": [
            "Льгота — по профилю олимпиады; ЕГЭ по предмету подтверждения не ниже 75 баллов.",
            "«100 баллов за ДВИ» — максимальный балл за дополнительное вступительное испытание МГУ.",
            f"«Любая олимпиада перечня» — по перечню {rsr_year} учебного года.",
        ],
        "defaults": {},
        "rules": rules,
    }
    parse_file(doc, catalog, args.out)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{table.title}\nфакультетов: {table.faculties}, строк: {table.rows}, правил: {len(table.rules)}, "
          f"сохранено: {len(rules)}")
    print(f"Записано: {args.out}")
    for title, items in (("Не разобрано", table.warnings), ("Не сопоставлено", problems)):
        if items:
            print(f"\n{title} ({len(items)}):")
            for p in items:
                print(f"  ? {p}")
    return 0


def cmd_benefits_import_mephi(args: argparse.Namespace) -> int:
    import json
    from datetime import date

    from app.benefits import parse_file, resolve_import
    from app.parsers.mephi_benefits import parse_mephi_html, resolve_numbers

    if args.source.startswith("http"):
        from app.fetch import fetch_html

        html = fetch_html(args.source)
    else:
        html = Path(args.source).read_text(encoding="utf-8")
    table = parse_mephi_html(html)
    catalog = _catalog()
    rules, problems = resolve_import(resolve_numbers(table.rules, catalog, args.rsr_year), catalog)
    doc = {
        "university": "МИФИ",
        "full_name": "Национальный исследовательский ядерный университет «МИФИ»",
        "city": "Москва",
        "admission_year": args.year,
        "document_status": "approved",
        "source_url": args.source if args.source.startswith("http") else args.url,
        "source_title": table.title or "Особые права победителей и призёров олимпиад, НИЯУ МИФИ",
        "checked_at": date.today().isoformat(),
        "notes": [
            "Льготы победителям и призёрам; ЕГЭ или экзамен МИФИ от 75 по предмету олимпиады.",
            "БВИ — за дипломы 10–11 класса, 100 баллов — за дипломы 8–11 класса.",
            "Кто не использовал право на БВИ, может засчитать 100 баллов по предмету олимпиады.",
            "Направления указаны кодами (01.03.02 и т. п.) — названия в правилах приёма МИФИ.",
        ],
        "defaults": {},
        "rules": rules,
    }
    parse_file(doc, catalog, args.out)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{table.title}\nстрок: {table.rows}, правил: {len(table.rules)}, сохранено: {len(rules)}")
    print(f"Записано: {args.out}")
    for title, items in (("Пропущено", table.warnings), ("Не сопоставлено", problems)):
        if items:
            print(f"\n{title} ({len(items)}):")
            for p in items:
                print(f"  ? {p}")
    return 0


def cmd_benefits_import_spbu(args: argparse.Namespace) -> int:
    import json
    from datetime import date

    from app.benefits import parse_file, resolve_import
    from app.parsers.spbu_benefits import parse_spbu_pdf

    table = parse_spbu_pdf(args.source)
    catalog = _catalog()
    rules, problems = resolve_import([{**r, "rsr_year": args.rsr_year} if r["olympiad"] == "*" else r
                                      for r in table.rules], catalog)
    doc = {
        "university": "СПбГУ",
        "full_name": "Санкт-Петербургский государственный университет",
        "city": "Санкт-Петербург",
        "admission_year": args.year,
        "document_status": "approved",
        "source_url": args.url,
        "source_title": table.title or "Особые права при приёме в СПбГУ",
        "checked_at": date.today().isoformat(),
        "notes": [
            "Засчитываются дипломы только за 10 и 11 класс; право действует 4 года после олимпиады.",
            "Нужен результат ВИ (ЕГЭ) не ниже указанного в правиле минимального балла.",
            f"«Любая олимпиада перечня» — по перечню {args.rsr_year} учебного года.",
        ],
        "defaults": {},
        "rules": rules,
    }
    parse_file(doc, catalog, args.out)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{table.title}\nстрок: {table.rows}, правил: {len(table.rules)}, сохранено: {len(rules)}")
    print(f"Записано: {args.out}")
    for title, items in (("Не разобрано", table.warnings), ("Не сопоставлено", problems)):
        if items:
            print(f"\n{title} ({len(items)}):")
            for p in items:
                print(f"  ? {p}")
    return 0


def cmd_benefits_import_itmo(args: argparse.Namespace) -> int:
    import json
    from datetime import date

    from app.benefits import parse_file, resolve_import
    from app.parsers.itmo_benefits import build_joiner, parse_rsosh_rows, parse_vsosh_rows, read_pdf_rows

    catalog = _catalog()
    files = {"bvi": args.bvi, "score100": args.score100, "vsosh": args.vsosh}
    loaded = {k: read_pdf_rows(v) for k, v in files.items() if v}
    join = build_joiner([text for _, text in loaded.values()], catalog)
    imports = []
    for kind in ("bvi", "score100"):
        if kind in loaded:
            imports.append(parse_rsosh_rows(loaded[kind][0], kind, join))
    if "vsosh" in loaded:
        vsosh = next(o for o in catalog.olympiads.values() if o.is_vsosh)
        imports.append(parse_vsosh_rows(loaded["vsosh"][0], join, sorted(vsosh.profiles)))
    raw = [r for imp in imports for r in imp.rules]
    rules, problems = resolve_import([{**r, "rsr_year": args.rsr_year} if r["olympiad"] == "*" else r
                                      for r in raw], catalog)
    doc = {
        "university": "ИТМО",
        "full_name": "Национальный исследовательский университет ИТМО",
        "city": "Санкт-Петербург",
        "admission_year": args.year,
        "document_status": "approved",
        "source_url": "https://abit.itmo.ru/bachelor",
        "source_title": f"Учёт дипломов олимпиад РСОШ и ВсОШ при поступлении в ИТМО в {args.year} году",
        "checked_at": date.today().isoformat(),
        "notes": [
            "Засчитываются дипломы РСОШ за 10 и 11 класс; ЕГЭ по предмету подтверждения — от 75 баллов.",
            "100 баллов дают олимпиады, которые не дают БВИ на это направление.",
            f"«Любая олимпиада перечня» — по перечню {args.rsr_year} учебного года.",
        ],
        "defaults": {},
        "rules": rules,
    }
    parse_file(doc, catalog, args.out)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"строк: {sum(i.rows for i in imports)}, правил: {len(raw)}, сохранено: {len(rules)}")
    print(f"Записано: {args.out}")
    warnings = [w for i in imports for w in i.warnings]
    for title, items in (("Не разобрано", warnings), ("Не сопоставлено", problems)):
        if items:
            print(f"\n{title} ({len(items)}):")
            for p in items:
                print(f"  ? {p}")
    return 0


def cmd_benefits_import_sechenov(args: argparse.Namespace) -> int:
    import json
    from datetime import date

    from app.benefits import parse_file, resolve_import
    from app.parsers.sechenov_benefits import parse_sechenov_pdf

    table = parse_sechenov_pdf(args.source)
    catalog = _catalog()
    rules, problems = resolve_import(table.rules, catalog)
    doc = {
        "university": "Сеченовский",
        "full_name": "Первый МГМУ имени И.М. Сеченова (Сеченовский Университет)",
        "city": "Москва",
        "admission_year": args.year,
        "document_status": "approved",
        "source_url": args.url,
        "source_title": table.title or "Перечень олимпиад школьников, Сеченовский Университет",
        "checked_at": date.today().isoformat(),
        "notes": [
            "БВИ по олимпиадам школьников Сеченовский не даёт — только 100 баллов по предмету профиля.",
            "Дипломы за 10 или 11 класс; ЕГЭ или внутреннее испытание по предмету подтверждения — от 75 баллов.",
            "100 баллов засчитываются на всех направлениях, где есть это вступительное испытание.",
        ],
        "defaults": {},
        "rules": rules,
    }
    parse_file(doc, catalog, args.out)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{table.title}\nстрок: {table.rows}, правил: {len(table.rules)}, сохранено: {len(rules)}")
    print(f"Записано: {args.out}")
    for title, items in (("Не разобрано", table.warnings), ("Не сопоставлено", problems)):
        if items:
            print(f"\n{title} ({len(items)}):")
            for p in items:
                print(f"  ? {p}")
    return 0


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
    benefits = _benefits(catalog, strict=False)
    storage = Storage(settings.bot_db_path)
    print(f"Каталог: {len(catalog.olympiads)} олимпиад, годы {', '.join(catalog.years)}. "
          f"Льготы: {len(benefits.latest())} вузов. База бота: {settings.bot_db_path}")
    print("Бот запущен, остановка — Ctrl+C")
    try:
        asyncio.run(run(token, catalog, storage, proxy=settings.telegram_proxy,
                        force_ipv4=settings.telegram_force_ipv4, stages_autosync=settings.stages_autosync,
                        benefits=benefits))
    except KeyboardInterrupt:
        pass
    finally:
        storage.close()
    return 0


def cmd_bot_check(args: argparse.Namespace) -> int:
    from app.bot.netcheck import run_check

    return run_check(settings.telegram_bot_token, settings.telegram_proxy)


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


def cmd_stages_sync(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from app.bot.notifier import STAGES_SYNC_KEY, moscow_now
    from app.bot.storage import Storage
    from app.fetch import fetch_html
    from app.stages_sync import sync_stages

    catalog = _catalog()
    storage = Storage(settings.bot_db_path)

    def progress(i: int, total: int, name: str) -> None:
        print(f"\r[{i}/{total}] {name[:70]:<70}", end="", flush=True)

    print(f"Загружаю расписания с olimpiada.ru, пауза между запросами {settings.request_delay:g} с. "
          "Первый запуск ищет все страницы олимпиад перечня (около 1100 страниц, до часа), "
          "следующие — только обновляют известные (около 300 страниц).")
    try:
        report = sync_stages(catalog, storage, fetch_html, moscow_now().date(),
                             full=args.full, limit=args.limit, progress=progress)
        if not args.limit:
            storage.set_meta(STAGES_SYNC_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    finally:
        storage.close()
    print("\n" + report.summary())
    for line in report.unmatched:
        print(f"  ? не нашёл в перечне: {line}")
    for line in report.errors:
        print(f"  ! {line}")
    return 0 if not report.errors else 1


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
    p = bot.add_parser("check", help="проверить связь с Telegram")
    p.set_defaults(func=cmd_bot_check)

    ben = groups.add_parser("benefits", help="льготы вузов").add_subparsers(dest="command", required=True)
    p = ben.add_parser("check", help="проверить файлы benefits/*.json")
    p.set_defaults(func=cmd_benefits_check)
    p = ben.add_parser("import-hse", help="импорт таблицы особых прав ВШЭ (xlsx: ссылка или файл)")
    p.add_argument("source")
    p.add_argument("--out", required=True, help="куда записать, например benefits/hse_moscow_2026.json")
    p.add_argument("--name", default="ВШЭ", help="короткое имя в боте (ВШЭ, ВШЭ — СПб, …)")
    p.add_argument("--campus", default="Москва")
    p.add_argument("--year", type=int, default=2026, help="год приёма, если его нет в заголовке таблицы")
    p.add_argument("--page", default="https://www.hse.ru/docs/1130197982.html", help="страница документа")
    p.set_defaults(func=cmd_benefits_import_hse)
    p = ben.add_parser("import-msu", help="импорт PDF особых прав МГУ (нужен pdfplumber)")
    p.add_argument("source", help="путь к olymp_benefits.pdf")
    p.add_argument("--out", required=True, help="например benefits/msu_2026.json")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--rsr-year", default="2025/26", help="учебный год перечня для «*»")
    p.add_argument("--url", default="https://cpk.msu.ru/files/2026/olymp_benefits.pdf")
    p.set_defaults(func=cmd_benefits_import_msu)
    p = ben.add_parser("import-spbu", help="импорт PDF особых прав СПбГУ (нужен pdfplumber)")
    p.add_argument("source", help="путь к bac_spec_olymp_2_<год>.pdf")
    p.add_argument("--out", default="benefits/spbu_2026.json")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--rsr-year", default="2025/26")
    p.add_argument("--url", default="https://abiturient.spbu.ru/medialibrary/ru/2026/bac/bac_spec_olymp_2_2026.pdf")
    p.set_defaults(func=cmd_benefits_import_spbu)
    p = ben.add_parser("import-itmo", help="импорт PDF особых прав ИТМО (нужен pdfplumber)")
    p.add_argument("--bvi", default="data/itmo_rsosh_bvi_2026.pdf")
    p.add_argument("--score100", default="data/itmo_rsosh_100_2026.pdf")
    p.add_argument("--vsosh", default="data/itmo_vsosh_2026.pdf")
    p.add_argument("--out", default="benefits/itmo_2026.json")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--rsr-year", default="2025/26")
    p.set_defaults(func=cmd_benefits_import_itmo)
    p = ben.add_parser("import-sechenov", help="импорт PDF перечня олимпиад Сеченовского (нужен pdfplumber)")
    p.add_argument("source", nargs="?", default="data/sechenov_olymp.pdf")
    p.add_argument("--out", default="benefits/sechenov_2026.json")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--url", default="https://www.sechenov.ru/upload/iblock/4f9/rqcqaakaep5gyyfvivy0mba980kmvrwm/"
                                    "Pravila-priema_2026_2027_BS_pril5_Perechen-olimpiad.pdf")
    p.set_defaults(func=cmd_benefits_import_sechenov)
    mephi_url = "https://admission.mephi.ru/admission/baccalaureate-and-specialty/specials/winners"
    p = ben.add_parser("import-mephi", help="импорт таблицы особых прав МИФИ (страница или сохранённый html)")
    p.add_argument("source", nargs="?", default=mephi_url)
    p.add_argument("--out", default="benefits/mephi_2026.json")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--rsr-year", default="2025/26")
    p.add_argument("--url", default=mephi_url)
    p.set_defaults(func=cmd_benefits_import_mephi)

    stages = groups.add_parser("stages", help="этапы олимпиад").add_subparsers(dest="command", required=True)
    p = stages.add_parser("import", help="загрузить этапы из CSV")
    p.add_argument("csv")
    p.set_defaults(func=cmd_stages_import)
    p = stages.add_parser("sync", help="загрузить даты этапов с olimpiada.ru")
    p.add_argument("--limit", type=int, help="проверить только первые N страниц (пробный запуск)")
    p.add_argument("--full", action="store_true", help="заново найти все страницы перечня через sitemap")
    p.set_defaults(func=cmd_stages_sync)
    p = stages.add_parser("list", help="показать загруженные этапы")
    p.set_defaults(func=cmd_stages_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
