"""Выполнение SQL через psql и простые миграции.

Миграции — файлы db/migrations/NNN_name.sql. Применённые версии записываются
в таблицу schema_migrations, каждая миграция выполняется в своей транзакции.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.config import PROJECT_ROOT, settings

MIGRATIONS_DIR = PROJECT_ROOT / "db" / "migrations"


class PsqlError(RuntimeError):
    pass


def run_psql(sql: str, *, database_url: str | None = None, single_transaction: bool = False,
             tuples_only: bool = False) -> str:
    psql = shutil.which("psql")
    if not psql:
        raise PsqlError("Не найден psql. Установите клиент PostgreSQL или используйте docker compose exec db psql")
    args = [psql, database_url or settings.database_url, "-X", "-q", "-v", "ON_ERROR_STOP=1"]
    if single_transaction:
        args.append("-1")
    if tuples_only:
        args += ["-t", "-A"]
    result = subprocess.run(args, input=sql, capture_output=True, text=True)
    if result.returncode != 0:
        raise PsqlError(result.stderr.strip() or f"psql завершился с кодом {result.returncode}")
    return result.stdout


def applied_migrations(database_url: str | None = None) -> set[str]:
    run_psql(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());",
        database_url=database_url,
    )
    out = run_psql("SELECT version FROM schema_migrations;", database_url=database_url, tuples_only=True)
    return {line.strip() for line in out.splitlines() if line.strip()}


def migrate(database_url: str | None = None) -> list[str]:
    """Применяет новые миграции по порядку. Возвращает список применённых."""
    done = applied_migrations(database_url)
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.stem
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        sql += f"\nINSERT INTO schema_migrations (version) VALUES ('{version}');\n"
        run_psql(sql, database_url=database_url, single_transaction=True)
        applied.append(version)
    return applied


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))
