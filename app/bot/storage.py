"""Хранилище бота на SQLite: пользователи, подписки, этапы, журнал уведомлений.

SQLite выбран для старта: не нужен отдельный сервер, файл лежит в data/bot.sqlite3,
бот работает и на домашнем компьютере. Храним минимум персональных данных:
только Telegram ID и подписки. При переезде на сервер эти же таблицы
переносятся в PostgreSQL (схема в db/migrations/001_init.sql).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from app.stages import Stage, StageRow

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_user (
    telegram_id  INTEGER PRIMARY KEY,
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscription (
    telegram_id  INTEGER NOT NULL REFERENCES bot_user(telegram_id) ON DELETE CASCADE,
    olympiad_id  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (telegram_id, olympiad_id)
);
CREATE INDEX IF NOT EXISTS subscription_olympiad_idx ON subscription (olympiad_id);
CREATE TABLE IF NOT EXISTS stage (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    olympiad_id    TEXT NOT NULL,
    academic_year  TEXT NOT NULL,
    profile        TEXT NOT NULL DEFAULT '',   -- '' = все профили (NULL ломал бы UNIQUE)
    kind           TEXT NOT NULL,
    title          TEXT NOT NULL,
    starts_at      TEXT,
    ends_at        TEXT,
    source_url     TEXT,
    external_id    TEXT,                       -- «olimpiada:<олимпиада>:<событие>» для загруженных с сайта
    created_at     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS stage_olympiad_idx ON stage (olympiad_id);
CREATE UNIQUE INDEX IF NOT EXISTS stage_external_idx ON stage (external_id) WHERE external_id IS NOT NULL;
-- ручные этапы (из CSV) уникальны по смыслу, загруженные с сайта — по id события
CREATE UNIQUE INDEX IF NOT EXISTS stage_manual_idx ON stage (olympiad_id, academic_year, kind, title, profile)
    WHERE external_id IS NULL;
CREATE TABLE IF NOT EXISTS university_subscription (  -- «сообщить, когда вуз выложит льготы»
    telegram_id  INTEGER NOT NULL REFERENCES bot_user(telegram_id) ON DELETE CASCADE,
    university   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (telegram_id, university)
);
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_query (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    query       TEXT NOT NULL UNIQUE,     -- текст запроса; в кнопку Telegram (64 байта) он не влезает
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_log (
    telegram_id  INTEGER NOT NULL,
    stage_id     INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (telegram_id, stage_id, kind)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


class Storage:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate_stage_table()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _migrate_stage_table(self) -> None:
        """База первой версии бота: у stage не было external_id, а UNIQUE был на всей таблице.
        Пересоздаём таблицу с сохранением строк (id остаются — журнал уведомлений не ломается)."""
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(stage)").fetchall()}
        if not columns or "external_id" in columns:
            return
        self.conn.executescript("""
            BEGIN;
            ALTER TABLE stage RENAME TO stage_old;
            DROP INDEX IF EXISTS stage_olympiad_idx;
            CREATE TABLE stage (
                id INTEGER PRIMARY KEY AUTOINCREMENT, olympiad_id TEXT NOT NULL, academic_year TEXT NOT NULL,
                profile TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, title TEXT NOT NULL,
                starts_at TEXT, ends_at TEXT, source_url TEXT, external_id TEXT, created_at TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO stage (id, olympiad_id, academic_year, profile, kind, title, starts_at, ends_at, source_url)
                SELECT id, olympiad_id, academic_year, profile, kind, title, starts_at, ends_at, source_url FROM stage_old;
            DROP TABLE stage_old;
            COMMIT;
        """)

    def close(self) -> None:
        self.conn.close()

    # -- пользователи и подписки --------------------------------------------

    def touch_user(self, telegram_id: int) -> None:
        """Регистрирует пользователя или снова делает его активным."""
        self.conn.execute(
            "INSERT INTO bot_user (telegram_id, created_at) VALUES (?, ?) "
            "ON CONFLICT (telegram_id) DO UPDATE SET is_active = 1",
            (telegram_id, _now()),
        )
        self.conn.commit()

    def deactivate_user(self, telegram_id: int) -> None:
        """Пользователь заблокировал бота: перестаём слать ему уведомления."""
        self.conn.execute("UPDATE bot_user SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
        self.conn.commit()

    def subscribe(self, telegram_id: int, olympiad_id: str) -> bool:
        """Возвращает True, если подписка новая."""
        self.touch_user(telegram_id)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO subscription (telegram_id, olympiad_id, created_at) VALUES (?, ?, ?)",
            (telegram_id, olympiad_id, _now()),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def unsubscribe(self, telegram_id: int, olympiad_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM subscription WHERE telegram_id = ? AND olympiad_id = ?", (telegram_id, olympiad_id)
        )
        self.conn.commit()
        return cur.rowcount == 1

    def is_subscribed(self, telegram_id: int, olympiad_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM subscription WHERE telegram_id = ? AND olympiad_id = ?", (telegram_id, olympiad_id)
        ).fetchone()
        return row is not None

    def subscriptions(self, telegram_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT olympiad_id FROM subscription WHERE telegram_id = ? ORDER BY created_at", (telegram_id,)
        ).fetchall()
        return [r["olympiad_id"] for r in rows]

    def subscribers_by_olympiad(self) -> dict[str, list[int]]:
        """Подписчики активных пользователей: id олимпиады -> Telegram ID."""
        rows = self.conn.execute(
            "SELECT s.olympiad_id, s.telegram_id FROM subscription s "
            "JOIN bot_user u ON u.telegram_id = s.telegram_id WHERE u.is_active = 1"
        ).fetchall()
        result: dict[str, list[int]] = {}
        for r in rows:
            result.setdefault(r["olympiad_id"], []).append(r["telegram_id"])
        return result

    def stats(self) -> dict[str, int]:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "users": q("SELECT count(*) FROM bot_user WHERE is_active = 1"),
            "subscriptions": q("SELECT count(*) FROM subscription"),
            "stages": q("SELECT count(*) FROM stage"),
        }

    # -- этапы ---------------------------------------------------------------

    def upsert_stages(self, rows: list[StageRow]) -> int:
        """Добавляет этапы или обновляет даты существующих. Возвращает число строк.

        Этапы с external_id (загруженные с сайта) узнаются по нему, ручные — по олимпиаде,
        году, виду, названию и профилю.
        """
        now = _now()
        for r in rows:
            values = (r.olympiad_id, r.academic_year, r.profile or "", r.kind, r.title,
                      r.starts_at.isoformat() if r.starts_at else None,
                      r.ends_at.isoformat() if r.ends_at else None, r.source_url, r.external_id, now)
            insert = ("INSERT INTO stage (olympiad_id, academic_year, profile, kind, title, starts_at, ends_at, "
                      "source_url, external_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ")
            if r.external_id:
                self.conn.execute(insert + """
                    ON CONFLICT (external_id) WHERE external_id IS NOT NULL DO UPDATE SET
                        olympiad_id = excluded.olympiad_id, academic_year = excluded.academic_year,
                        profile = excluded.profile, kind = excluded.kind, title = excluded.title,
                        starts_at = excluded.starts_at, ends_at = excluded.ends_at, source_url = excluded.source_url
                """, values)
            else:
                self.conn.execute(insert + """
                    ON CONFLICT (olympiad_id, academic_year, kind, title, profile) WHERE external_id IS NULL
                    DO UPDATE SET starts_at = excluded.starts_at, ends_at = excluded.ends_at,
                        source_url = excluded.source_url
                """, values)
        self.conn.commit()
        return len(rows)

    def replace_external_stages(self, prefix: str, rows: list[StageRow]) -> tuple[int, int]:
        """Синхронизация одного источника: обновляет этапы с external_id, начинающимся с prefix,
        и удаляет те, что пропали с сайта. Возвращает (сохранено, удалено)."""
        self.upsert_stages(rows)
        keep = [r.external_id for r in rows if r.external_id]
        placeholders = ",".join("?" * len(keep)) or "''"
        cur = self.conn.execute(
            f"DELETE FROM stage WHERE external_id LIKE ? AND external_id NOT IN ({placeholders})",
            (prefix + "%", *keep),
        )
        self.conn.commit()
        return len(rows), cur.rowcount

    def subscribe_university(self, telegram_id: int, university: str) -> bool:
        self.touch_user(telegram_id)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO university_subscription (telegram_id, university, created_at) VALUES (?, ?, ?)",
            (telegram_id, university, _now()))
        self.conn.commit()
        return cur.rowcount > 0

    def unsubscribe_university(self, telegram_id: int, university: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM university_subscription WHERE telegram_id = ? AND university = ?", (telegram_id, university))
        self.conn.commit()
        return cur.rowcount > 0

    def is_subscribed_university(self, telegram_id: int, university: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM university_subscription WHERE telegram_id = ? AND university = ?",
            (telegram_id, university)).fetchone() is not None

    def university_subscribers(self, university: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT s.telegram_id FROM university_subscription s JOIN bot_user u USING (telegram_id) "
            "WHERE s.university = ? AND u.is_active = 1 ORDER BY s.telegram_id", (university,))
        return [r[0] for r in rows]

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                          "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
        self.conn.commit()

    def subscriptions_since(self) -> list[tuple[int, str, str]]:
        """(telegram_id, olympiad_id, когда подписался) для активных пользователей."""
        rows = self.conn.execute(
            "SELECT s.telegram_id, s.olympiad_id, s.created_at FROM subscription s "
            "JOIN bot_user u ON u.telegram_id = s.telegram_id WHERE u.is_active = 1"
        ).fetchall()
        return [(r["telegram_id"], r["olympiad_id"], r["created_at"]) for r in rows]

    def stages(self, olympiad_id: str | None = None) -> list[Stage]:
        sql = "SELECT * FROM stage"
        params: tuple = ()
        if olympiad_id:
            sql += " WHERE olympiad_id = ?"
            params = (olympiad_id,)
        sql += " ORDER BY coalesce(starts_at, ends_at), id"
        return [
            Stage(
                id=r["id"], olympiad_id=r["olympiad_id"], kind=r["kind"], title=r["title"],
                starts_at=_date(r["starts_at"]), ends_at=_date(r["ends_at"]),
                academic_year=r["academic_year"], profile=r["profile"] or None, source_url=r["source_url"],
                created_at=r["created_at"] or "",
            )
            for r in self.conn.execute(sql, params).fetchall()
        ]

    def upcoming_stages(self, olympiad_id: str, today: date, limit: int = 5) -> list[Stage]:
        return [s for s in self.stages(olympiad_id) if s.is_upcoming(today)][:limit]

    # -- поисковые запросы (для кнопок «Назад» и листания страниц) -----------

    def save_query(self, query: str) -> int:
        self.conn.execute(
            "INSERT OR IGNORE INTO search_query (query, created_at) VALUES (?, ?)", (query, _now())
        )
        self.conn.commit()
        return self.conn.execute("SELECT id FROM search_query WHERE query = ?", (query,)).fetchone()["id"]

    def get_query(self, query_id: int) -> str | None:
        row = self.conn.execute("SELECT query FROM search_query WHERE id = ?", (query_id,)).fetchone()
        return row["query"] if row else None

    # -- журнал уведомлений --------------------------------------------------

    def sent_notifications(self) -> set[tuple[int, int, str]]:
        rows = self.conn.execute("SELECT telegram_id, stage_id, kind FROM notification_log").fetchall()
        return {(r["telegram_id"], r["stage_id"], r["kind"]) for r in rows}

    def log_notification(self, telegram_id: int, stage_id: int, kind: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO notification_log (telegram_id, stage_id, kind, sent_at) VALUES (?, ?, ?, ?)",
            (telegram_id, stage_id, kind, _now()),
        )
        self.conn.commit()
