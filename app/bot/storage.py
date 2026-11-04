"""Хранилище бота на SQLite: пользователи, подписки, этапы, журнал уведомлений.

SQLite выбран для старта: не нужен отдельный сервер, файл лежит в data/bot.sqlite3,
бот работает и на домашнем компьютере. Храним минимум персональных данных:
только Telegram ID и подписки. При переезде на сервер эти же таблицы
переносятся в PostgreSQL (схема в db/migrations/001_init.sql).
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
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
-- личный кабинет на сайте: вход через бота, профиль и дипломы
CREATE TABLE IF NOT EXISTS web_login (
    token        TEXT PRIMARY KEY,
    telegram_id  INTEGER,
    created_at   TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS web_session (
    sid          TEXT PRIMARY KEY,
    telegram_id  INTEGER NOT NULL REFERENCES bot_user(telegram_id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_profile (
    telegram_id  INTEGER PRIMARY KEY REFERENCES bot_user(telegram_id) ON DELETE CASCADE,
    name         TEXT NOT NULL DEFAULT '',
    grade        INTEGER,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_diploma (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id   INTEGER NOT NULL REFERENCES bot_user(telegram_id) ON DELETE CASCADE,
    olympiad_id   TEXT NOT NULL,
    profile       TEXT NOT NULL,
    diploma       TEXT NOT NULL,          -- winner | prize
    grade         INTEGER NOT NULL,
    academic_year TEXT NOT NULL,          -- год перечня, когда получен диплом: 2025/26
    created_at    TEXT NOT NULL,
    UNIQUE (telegram_id, olympiad_id, profile, academic_year)
);
CREATE TABLE IF NOT EXISTS help_request (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id   INTEGER,                -- если человек вошёл на сайт или написал из бота
    username      TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    contact       TEXT NOT NULL DEFAULT '',
    grade         INTEGER,
    goal          TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL,          -- site | bot
    created_at    TEXT NOT NULL,
    forwarded_at  TEXT                    -- когда бот переслал заявку админу
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
-- сопровождение (CRM): куратор включает его пользователю по короткому ID из кабинета
CREATE TABLE IF NOT EXISTS user_code (
    telegram_id  INTEGER PRIMARY KEY REFERENCES bot_user(telegram_id) ON DELETE CASCADE,
    code         TEXT NOT NULL UNIQUE           -- 6 цифр: его видно в кабинете, по нему куратор находит клиента
);
CREATE TABLE IF NOT EXISTS crm_group (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_client (
    telegram_id  INTEGER PRIMARY KEY REFERENCES bot_user(telegram_id) ON DELETE CASCADE,
    group_id     INTEGER REFERENCES crm_group(id) ON DELETE SET NULL,
    notes        TEXT NOT NULL DEFAULT '',      -- заметки куратора, клиент их не видит
    share_token  TEXT NOT NULL UNIQUE,          -- ссылка «только просмотр» для родителей
    notify       INTEGER NOT NULL DEFAULT 1,    -- сообщать клиенту в Telegram о смене статусов
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_goal (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER NOT NULL REFERENCES crm_client(telegram_id) ON DELETE CASCADE,
    university   TEXT NOT NULL,
    program      TEXT NOT NULL DEFAULT '',      -- '' — любая программа вуза
    created_at   TEXT NOT NULL,
    UNIQUE (telegram_id, university, program)
);
CREATE TABLE IF NOT EXISTS crm_olympiad (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id   INTEGER NOT NULL REFERENCES crm_client(telegram_id) ON DELETE CASCADE,
    olympiad_id   TEXT NOT NULL,
    profile       TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'todo',
    reg_deadline  TEXT,                         -- вручную; пусто — берём конец регистрации с сайта олимпиады
    updated_at    TEXT NOT NULL,
    UNIQUE (telegram_id, olympiad_id, profile)
);
CREATE TABLE IF NOT EXISTS crm_event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER NOT NULL REFERENCES crm_client(telegram_id) ON DELETE CASCADE,
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_access (            -- логин и пароль от кабинета олимпиады (зашифрованы)
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id    INTEGER NOT NULL REFERENCES crm_client(telegram_id) ON DELETE CASCADE,
    crm_olympiad   INTEGER NOT NULL UNIQUE REFERENCES crm_olympiad(id) ON DELETE CASCADE,
    login_enc      TEXT NOT NULL,
    password_enc   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',   -- pending | ok | bad
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    checked_at     TEXT
);
CREATE TABLE IF NOT EXISTS crm_access_log (        -- кто и когда смотрел пароль
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER NOT NULL,
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_reminder (          -- отправленные напоминания сопровождения (без повторов)
    crm_olympiad  INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    sent_at       TEXT NOT NULL,
    PRIMARY KEY (crm_olympiad, kind)
);
CREATE TABLE IF NOT EXISTS news (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,                 -- registration_open | registration_closing | stage_dates | digest | article
    dedupe_key    TEXT UNIQUE,                   -- автоматические новости не повторяются
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    olympiad_id   TEXT,
    profile       TEXT NOT NULL DEFAULT '',
    source_url    TEXT,
    event_date    TEXT,
    status        TEXT NOT NULL DEFAULT 'draft', -- draft | published | rejected
    created_at    TEXT NOT NULL,
    published_at  TEXT,
    posted_at     TEXT                           -- когда ушла в Telegram-канал
);
CREATE INDEX IF NOT EXISTS news_status_idx ON news (status, published_at);
CREATE TABLE IF NOT EXISTS uni_request (             -- «Предложить вуз»: каких вузов не хватает
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER,
    university   TEXT NOT NULL,
    program      TEXT NOT NULL DEFAULT '',
    contact      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'new',    -- new | done
    created_at   TEXT NOT NULL
);
-- аналитика для админки
CREATE TABLE IF NOT EXISTS pageview (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    day          TEXT NOT NULL,
    visitor      TEXT NOT NULL,                 -- случайный id браузера (cookie bv_vid)
    telegram_id  INTEGER,                       -- если вошёл на сайт
    path         TEXT NOT NULL,
    ref_host     TEXT NOT NULL DEFAULT '',
    utm_source   TEXT NOT NULL DEFAULT '',
    utm_campaign TEXT NOT NULL DEFAULT '',
    device       TEXT NOT NULL DEFAULT ''       -- mobile | desktop
);
CREATE INDEX IF NOT EXISTS pageview_day_idx ON pageview (day);
CREATE INDEX IF NOT EXISTS pageview_visitor_idx ON pageview (visitor);
CREATE TABLE IF NOT EXISTS visitor (                -- первое касание: откуда пришёл
    visitor      TEXT PRIMARY KEY,
    first_ts     TEXT NOT NULL,
    ref_host     TEXT NOT NULL DEFAULT '',
    utm_source   TEXT NOT NULL DEFAULT '',
    utm_campaign TEXT NOT NULL DEFAULT '',
    telegram_id  INTEGER
);
CREATE TABLE IF NOT EXISTS bot_activity (           -- активность в боте по дням
    telegram_id  INTEGER NOT NULL,
    day          TEXT NOT NULL,
    actions      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (telegram_id, day)
);
CREATE TABLE IF NOT EXISTS search_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    source       TEXT NOT NULL,                 -- site | bot
    query        TEXT NOT NULL,
    results      INTEGER NOT NULL,
    telegram_id  INTEGER
);
CREATE TABLE IF NOT EXISTS auth_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    telegram_id  INTEGER NOT NULL,
    visitor      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS outbox (               -- сообщения с сайта, которые бот отправит в Telegram
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER NOT NULL,
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    sent_at      TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ago(*, minutes: int = 0, days: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes, days=days)).isoformat(timespec="seconds")


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
        # бот и сайт пишут в одну базу: подождать до 5 секунд, а не падать с «database is locked»
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate_stage_table()
        self.conn.executescript(SCHEMA)
        self._add_column("crm_olympiad", "plan", "TEXT NOT NULL DEFAULT '[]'")
        for column in ("username", "first_name", "last_name", "phone", "phone_at", "last_seen"):
            self._add_column("bot_user", column, "TEXT")
        self._add_column("web_session", "last_seen", "TEXT")
        self._add_column("news", "category", "TEXT NOT NULL DEFAULT ''")
        self._add_column("news", "sources", "TEXT NOT NULL DEFAULT '[]'")
        # новости, созданные до появления разделов, — раздел по виду новости
        self.conn.execute(
            "UPDATE news SET category = CASE kind WHEN 'article' THEN 'guides' WHEN 'digest' THEN 'digest' "
            "WHEN 'benefits' THEN 'benefits' ELSE 'stages' END WHERE category = ''")
        self.conn.commit()

    def _add_column(self, table: str, column: str, decl: str) -> None:
        """Новая колонка в таблице из прошлой версии (CREATE TABLE IF NOT EXISTS её не добавит)."""
        columns = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")  # noqa: S608

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

    # -- сайт: вход через бота ---------------------------------------------

    def create_login(self, token: str) -> None:
        self.conn.execute("INSERT INTO web_login (token, created_at) VALUES (?, ?)", (token, _now()))
        self.conn.commit()

    def confirm_login(self, token: str, telegram_id: int, max_age_minutes: int = 15) -> bool:
        """Бот получил /start login_<token>: привязываем вход к пользователю (токен живёт 15 минут)."""
        self.touch_user(telegram_id)
        cur = self.conn.execute(
            "UPDATE web_login SET telegram_id = ?, confirmed_at = ? WHERE token = ? AND telegram_id IS NULL "
            "AND created_at >= ?", (telegram_id, _now(), token, _ago(minutes=max_age_minutes)))
        self.conn.commit()
        return cur.rowcount > 0

    def take_login(self, token: str) -> int | None:
        """Сайт забирает подтверждённый вход — один раз."""
        row = self.conn.execute(
            "SELECT telegram_id FROM web_login WHERE token = ? AND telegram_id IS NOT NULL", (token,)).fetchone()
        if row is None:
            return None
        self.conn.execute("DELETE FROM web_login WHERE token = ?", (token,))
        self.conn.execute("DELETE FROM web_login WHERE created_at < ?", (_ago(minutes=60),))
        self.conn.commit()
        return row[0]

    def create_session(self, sid: str, telegram_id: int) -> None:
        self.conn.execute("INSERT INTO web_session (sid, telegram_id, created_at) VALUES (?, ?, ?)",
                          (sid, telegram_id, _now()))
        self.conn.commit()

    def session_user(self, sid: str, max_age_days: int = 30) -> int | None:
        """Сессия живёт max_age_days с последнего захода (скользящий срок): заходишь — продлевается."""
        row = self.conn.execute(
            "SELECT telegram_id, coalesce(last_seen, created_at) AS seen FROM web_session WHERE sid = ? "
            "AND coalesce(last_seen, created_at) >= ?", (sid, _ago(days=max_age_days))).fetchone()
        if row is None:
            return None
        if row["seen"] < _ago(minutes=60):          # продлеваем не чаще раза в час — меньше записей в базу
            self.conn.execute("UPDATE web_session SET last_seen = ? WHERE sid = ?", (_now(), sid))
            self.conn.commit()
        return row["telegram_id"]

    def delete_session(self, sid: str) -> None:
        self.conn.execute("DELETE FROM web_session WHERE sid = ?", (sid,))
        self.conn.commit()

    # -- профиль и дипломы ---------------------------------------------------

    def profile(self, telegram_id: int) -> dict:
        row = self.conn.execute("SELECT name, grade FROM user_profile WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return {"name": row["name"], "grade": row["grade"]} if row else {"name": "", "grade": None}

    def save_profile(self, telegram_id: int, name: str, grade: int | None) -> None:
        self.touch_user(telegram_id)
        self.conn.execute(
            "INSERT INTO user_profile (telegram_id, name, grade, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (telegram_id) DO UPDATE SET name = excluded.name, grade = excluded.grade, "
            "updated_at = excluded.updated_at", (telegram_id, name, grade, _now()))
        self.conn.commit()

    def add_diploma(self, telegram_id: int, olympiad_id: str, profile: str, diploma: str, grade: int,
                    academic_year: str) -> None:
        self.touch_user(telegram_id)
        self.conn.execute(
            "INSERT INTO user_diploma (telegram_id, olympiad_id, profile, diploma, grade, academic_year, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (telegram_id, olympiad_id, profile, academic_year) "
            "DO UPDATE SET diploma = excluded.diploma, grade = excluded.grade",
            (telegram_id, olympiad_id, profile, diploma, grade, academic_year, _now()))
        self.conn.commit()

    def diplomas(self, telegram_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, olympiad_id, profile, diploma, grade, academic_year FROM user_diploma "
            "WHERE telegram_id = ? ORDER BY academic_year DESC, id", (telegram_id,))
        return [dict(r) for r in rows]

    def delete_diploma(self, telegram_id: int, diploma_id: int) -> None:
        self.conn.execute("DELETE FROM user_diploma WHERE id = ? AND telegram_id = ?", (diploma_id, telegram_id))
        self.conn.commit()

    def university_subscriptions(self, telegram_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT university FROM university_subscription WHERE telegram_id = ? ORDER BY created_at", (telegram_id,))
        return [r[0] for r in rows]

    # -- заявки на сопровождение -------------------------------------------

    def add_help_request(self, *, source: str, telegram_id: int | None = None, username: str = "", name: str = "",
                         contact: str = "", grade: int | None = None, goal: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO help_request (telegram_id, username, name, contact, grade, goal, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (telegram_id, username[:64], name[:100], contact[:200], grade, goal[:2000], source, _now()))
        self.conn.commit()
        return cur.lastrowid

    def recent_help_request(self, *, telegram_id: int | None = None, contact: str = "", hours: int = 24) -> bool:
        """Была ли заявка за последние сутки — чтобы одну и ту же не слали десять раз."""
        since = _ago(minutes=hours * 60)
        if telegram_id:
            row = self.conn.execute("SELECT 1 FROM help_request WHERE telegram_id = ? AND created_at >= ?",
                                    (telegram_id, since)).fetchone()
            if row:
                return True
        if contact:
            row = self.conn.execute("SELECT 1 FROM help_request WHERE lower(contact) = lower(?) AND created_at >= ?",
                                    (contact, since)).fetchone()
            return row is not None
        return False

    def help_requests_since(self, minutes: int) -> int:
        return self.conn.execute("SELECT count(*) FROM help_request WHERE created_at >= ?",
                                 (_ago(minutes=minutes),)).fetchone()[0]

    def pending_help_requests(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM help_request WHERE forwarded_at IS NULL ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def mark_help_forwarded(self, request_id: int) -> None:
        self.conn.execute("UPDATE help_request SET forwarded_at = ? WHERE id = ?", (_now(), request_id))
        self.conn.commit()

    def delete_user_data(self, telegram_id: int) -> None:
        """«Удалить мои данные» в кабинете: профиль, дипломы, подписки, сессии, заявки."""
        for table in ("user_diploma", "user_profile", "web_session", "subscription", "university_subscription",
                      "help_request", "crm_client", "user_code", "outbox", "uni_request", "bot_activity", "search_log",
                      "auth_log"):
            self.conn.execute(f"DELETE FROM {table} WHERE telegram_id = ?", (telegram_id,))  # noqa: S608
        # просмотры остаются в статистике обезличенными
        self.conn.execute("UPDATE pageview SET telegram_id = NULL WHERE telegram_id = ?", (telegram_id,))
        self.conn.execute("UPDATE visitor SET telegram_id = NULL WHERE telegram_id = ?", (telegram_id,))
        self.conn.execute("UPDATE bot_user SET username = NULL, first_name = NULL, last_name = NULL, phone = NULL, "
                          "phone_at = NULL WHERE telegram_id = ?", (telegram_id,))
        self.conn.commit()

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

    # -- сопровождение (CRM) -------------------------------------------------

    def user_code(self, telegram_id: int) -> str:
        """Короткий ID пользователя для куратора (создаётся при первом входе в кабинет)."""
        row = self.conn.execute("SELECT code FROM user_code WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if row:
            return row["code"]
        self.touch_user(telegram_id)
        while True:
            code = str(secrets.randbelow(900000) + 100000)
            try:
                self.conn.execute("INSERT INTO user_code (telegram_id, code) VALUES (?, ?)", (telegram_id, code))
                self.conn.commit()
                return code
            except sqlite3.IntegrityError:
                continue  # такой код уже занят — берём другой

    def user_by_code(self, code: str) -> int | None:
        row = self.conn.execute("SELECT telegram_id FROM user_code WHERE code = ?",
                                ("".join(ch for ch in code if ch.isdigit()),)).fetchone()
        return row["telegram_id"] if row else None

    def crm_enable(self, telegram_id: int) -> bool:
        """Включить сопровождение. True — если включили только что."""
        self.touch_user(telegram_id)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO crm_client (telegram_id, share_token, created_at) VALUES (?, ?, ?)",
            (telegram_id, secrets.token_urlsafe(16), _now()))
        self.conn.commit()
        return cur.rowcount == 1

    def crm_disable(self, telegram_id: int) -> None:
        self.conn.execute("DELETE FROM crm_client WHERE telegram_id = ?", (telegram_id,))
        self.conn.commit()

    def crm_client(self, telegram_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT c.*, g.name AS group_name, p.name, p.grade, uc.code FROM crm_client c "
            "LEFT JOIN crm_group g ON g.id = c.group_id LEFT JOIN user_profile p ON p.telegram_id = c.telegram_id "
            "LEFT JOIN user_code uc ON uc.telegram_id = c.telegram_id WHERE c.telegram_id = ?",
            (telegram_id,)).fetchone()
        return dict(row) if row else None

    def crm_client_by_token(self, token: str) -> int | None:
        row = self.conn.execute("SELECT telegram_id FROM crm_client WHERE share_token = ?", (token,)).fetchone()
        return row["telegram_id"] if row else None

    def crm_clients(self, group_id: int | None = None) -> list[dict]:
        sql = ("SELECT c.telegram_id, c.group_id, c.created_at, g.name AS group_name, p.name, p.grade, uc.code "
               "FROM crm_client c LEFT JOIN crm_group g ON g.id = c.group_id "
               "LEFT JOIN user_profile p ON p.telegram_id = c.telegram_id "
               "LEFT JOIN user_code uc ON uc.telegram_id = c.telegram_id")
        params: tuple = ()
        if group_id:
            sql += " WHERE c.group_id = ?"
            params = (group_id,)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY c.created_at DESC", params).fetchall()]

    def crm_update_client(self, telegram_id: int, **fields) -> None:
        allowed = {"group_id", "notes", "notify"}
        for key, value in fields.items():
            if key in allowed:
                self.conn.execute(f"UPDATE crm_client SET {key} = ? WHERE telegram_id = ?",  # noqa: S608
                                  (value, telegram_id))
        self.conn.commit()

    def crm_groups(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT g.id, g.name, count(c.telegram_id) AS size FROM crm_group g "
            "LEFT JOIN crm_client c ON c.group_id = g.id GROUP BY g.id ORDER BY g.name").fetchall()
        return [dict(r) for r in rows]

    def crm_add_group(self, name: str) -> int:
        self.conn.execute("INSERT OR IGNORE INTO crm_group (name, created_at) VALUES (?, ?)", (name, _now()))
        self.conn.commit()
        return self.conn.execute("SELECT id FROM crm_group WHERE name = ?", (name,)).fetchone()["id"]

    def crm_delete_group(self, group_id: int) -> None:
        self.conn.execute("DELETE FROM crm_group WHERE id = ?", (group_id,))
        self.conn.commit()

    def crm_group_members(self, group_id: int) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT telegram_id FROM crm_client WHERE group_id = ?", (group_id,))]

    def crm_goals(self, telegram_id: int) -> list[dict]:
        rows = self.conn.execute("SELECT id, university, program FROM crm_goal WHERE telegram_id = ? ORDER BY id",
                                 (telegram_id,)).fetchall()
        return [dict(r) for r in rows]

    def crm_add_goal(self, telegram_id: int, university: str, program: str) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO crm_goal (telegram_id, university, program, created_at) "
                                "VALUES (?, ?, ?, ?)", (telegram_id, university, program, _now()))
        self.conn.commit()
        return cur.rowcount == 1

    def crm_delete_goal(self, telegram_id: int, goal_id: int) -> None:
        self.conn.execute("DELETE FROM crm_goal WHERE id = ? AND telegram_id = ?", (goal_id, telegram_id))
        self.conn.commit()

    def crm_olympiads(self, telegram_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, olympiad_id, profile, status, reg_deadline, plan, updated_at FROM crm_olympiad "
            "WHERE telegram_id = ? ORDER BY id", (telegram_id,)).fetchall()
        return [dict(r, reg_deadline=_date(r["reg_deadline"])) for r in rows]

    def crm_olympiad(self, telegram_id: int, row_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM crm_olympiad WHERE id = ? AND telegram_id = ?",
                                (row_id, telegram_id)).fetchone()
        return dict(row, reg_deadline=_date(row["reg_deadline"])) if row else None

    def crm_add_olympiad(self, telegram_id: int, olympiad_id: str, profile: str) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO crm_olympiad (telegram_id, olympiad_id, profile, updated_at) VALUES (?, ?, ?, ?)",
            (telegram_id, olympiad_id, profile, _now()))
        self.conn.commit()
        return cur.rowcount == 1

    def crm_delete_olympiad(self, telegram_id: int, row_id: int) -> None:
        self.conn.execute("DELETE FROM crm_olympiad WHERE id = ? AND telegram_id = ?", (row_id, telegram_id))
        self.conn.commit()

    def crm_set_status(self, telegram_id: int, row_id: int, status: str) -> None:
        self.conn.execute("UPDATE crm_olympiad SET status = ?, updated_at = ? WHERE id = ? AND telegram_id = ?",
                          (status, _now(), row_id, telegram_id))
        self.conn.commit()

    def crm_set_deadline(self, telegram_id: int, row_id: int, deadline: date | None) -> None:
        self.conn.execute("UPDATE crm_olympiad SET reg_deadline = ?, updated_at = ? WHERE id = ? AND telegram_id = ?",
                          (deadline.isoformat() if deadline else None, _now(), row_id, telegram_id))
        self.conn.commit()

    def crm_log(self, telegram_id: int, text: str) -> None:
        self.conn.execute("INSERT INTO crm_event (telegram_id, text, created_at) VALUES (?, ?, ?)",
                          (telegram_id, text[:500], _now()))
        self.conn.commit()

    def crm_events(self, telegram_id: int, limit: int = 30) -> list[dict]:
        rows = self.conn.execute("SELECT text, created_at FROM crm_event WHERE telegram_id = ? "
                                 "ORDER BY id DESC LIMIT ?", (telegram_id, limit)).fetchall()
        return [dict(r) for r in rows]

    # -- исходящие сообщения (сайт → бот → Telegram) -----------------------

    def queue_message(self, telegram_id: int, text: str) -> None:
        self.conn.execute("INSERT INTO outbox (telegram_id, text, created_at) VALUES (?, ?, ?)",
                          (telegram_id, text, _now()))
        self.conn.commit()

    def pending_messages(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT id, telegram_id, text FROM outbox WHERE sent_at IS NULL ORDER BY id LIMIT ?",
                                 (limit,)).fetchall()
        return [dict(r) for r in rows]

    def mark_message_sent(self, message_id: int) -> None:
        self.conn.execute("UPDATE outbox SET sent_at = ? WHERE id = ?", (_now(), message_id))
        self.conn.commit()

    def crm_save_plan(self, row_id: int, plan: str) -> None:
        """Снимок этапов олимпиады: прошедшие этапы пропадают с сайта, а в роадмапе должны остаться."""
        self.conn.execute("UPDATE crm_olympiad SET plan = ? WHERE id = ?", (plan, row_id))
        self.conn.commit()

    # -- доступы к кабинетам олимпиад --------------------------------------

    def crm_accesses(self, telegram_id: int | None = None, status: str | None = None) -> list[dict]:
        sql = ("SELECT a.*, o.olympiad_id, o.profile, uc.code, p.name FROM crm_access a "
               "JOIN crm_olympiad o ON o.id = a.crm_olympiad "
               "LEFT JOIN user_code uc ON uc.telegram_id = a.telegram_id "
               "LEFT JOIN user_profile p ON p.telegram_id = a.telegram_id WHERE 1 = 1")
        params: list = []
        if telegram_id is not None:
            sql += " AND a.telegram_id = ?"
            params.append(telegram_id)
        if status:
            sql += " AND a.status = ?"
            params.append(status)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY a.updated_at", params).fetchall()]

    def crm_access(self, access_id: int) -> dict | None:
        rows = [a for a in self.crm_accesses() if a["id"] == access_id]
        return rows[0] if rows else None

    def crm_save_access(self, telegram_id: int, row_id: int, login_enc: str, password_enc: str) -> None:
        self.conn.execute(
            "INSERT INTO crm_access (telegram_id, crm_olympiad, login_enc, password_enc, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?) ON CONFLICT (crm_olympiad) DO UPDATE SET "
            "login_enc = excluded.login_enc, password_enc = excluded.password_enc, status = 'pending', "
            "updated_at = excluded.updated_at, checked_at = NULL",
            (telegram_id, row_id, login_enc, password_enc, _now(), _now()))
        self.conn.commit()

    def crm_delete_access(self, telegram_id: int, row_id: int) -> None:
        self.conn.execute("DELETE FROM crm_access WHERE telegram_id = ? AND crm_olympiad = ?", (telegram_id, row_id))
        self.conn.commit()

    def crm_set_access_status(self, access_id: int, status: str) -> None:
        self.conn.execute("UPDATE crm_access SET status = ?, checked_at = ? WHERE id = ?", (status, _now(), access_id))
        self.conn.commit()

    def crm_purge_accesses(self, before: str) -> int:
        """Удалить доступы, введённые раньше даты (ISO): конец сезона."""
        cur = self.conn.execute("DELETE FROM crm_access WHERE created_at < ?", (before,))
        self.conn.commit()
        return cur.rowcount

    def crm_access_log_add(self, telegram_id: int, text: str) -> None:
        self.conn.execute("INSERT INTO crm_access_log (telegram_id, text, created_at) VALUES (?, ?, ?)",
                          (telegram_id, text, _now()))
        self.conn.commit()

    def crm_access_log(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute("SELECT text, created_at FROM crm_access_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def crm_all_olympiads(self) -> list[dict]:
        """Олимпиады всех клиентов (для напоминаний)."""
        rows = self.conn.execute(
            "SELECT o.id, o.telegram_id, o.olympiad_id, o.profile, o.status, o.reg_deadline, o.plan, "
            "a.status AS access FROM crm_olympiad o LEFT JOIN crm_access a ON a.crm_olympiad = o.id").fetchall()
        return [dict(r, reg_deadline=_date(r["reg_deadline"])) for r in rows]

    def crm_reminder_sent(self, row_id: int, kind: str) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO crm_reminder (crm_olympiad, kind, sent_at) VALUES (?, ?, ?)",
                                (row_id, kind, _now()))
        self.conn.commit()
        return cur.rowcount == 0

    # -- новости ----------------------------------------------------------------

    def add_news(self, *, kind: str, title: str, body: str, dedupe_key: str | None = None,
                 olympiad_id: str | None = None, profile: str = "", source_url: str | None = None,
                 event_date: date | None = None, status: str = "draft", category: str = "",
                 sources: str = "[]") -> int | None:
        """Новая новость; None — такая (по dedupe_key) уже была."""
        if not category:
            category = {"article": "guides", "digest": "digest", "benefits": "benefits"}.get(kind, "stages")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO news (kind, dedupe_key, title, body, olympiad_id, profile, source_url, event_date, "
            "status, created_at, published_at, category, sources) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, dedupe_key, title[:300], body[:20000], olympiad_id, profile, source_url,
             event_date.isoformat() if event_date else None, status, _now(), _now() if status == "published" else None,
             category, sources))
        self.conn.commit()
        return cur.lastrowid if cur.rowcount == 1 else None

    def news_item(self, news_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM news WHERE id = ?", (news_id,)).fetchone()
        return dict(row) if row else None

    def news_list(self, status: str = "published", limit: int = 30, offset: int = 0,
                  olympiad_ids: list[str] | None = None, category: str = "") -> list[dict]:
        sql = "SELECT * FROM news WHERE status = ?"
        params: list = [status]
        if category:
            sql += " AND category = ?"
            params.append(category)
        if olympiad_ids is not None:
            if not olympiad_ids:
                return []
            sql += f" AND olympiad_id IN ({','.join('?' * len(olympiad_ids))})"
            params += olympiad_ids
        order = "published_at DESC, id DESC" if status == "published" else "created_at DESC, id DESC"
        rows = self.conn.execute(f"{sql} ORDER BY {order} LIMIT ? OFFSET ?", (*params, limit, offset))  # noqa: S608
        return [dict(r) for r in rows]

    def news_count(self, status: str) -> int:
        return self.conn.execute("SELECT count(*) FROM news WHERE status = ?", (status,)).fetchone()[0]

    def update_news(self, news_id: int, *, title: str | None = None, body: str | None = None,
                    status: str | None = None, category: str | None = None, sources: str | None = None) -> None:
        item = self.news_item(news_id)
        if item is None:
            return
        published_at = item["published_at"]
        if status == "published" and not published_at:
            published_at = _now()
        self.conn.execute(
            "UPDATE news SET title = ?, body = ?, status = ?, published_at = ?, category = ?, sources = ? WHERE id = ?",
            (title if title is not None else item["title"], body if body is not None else item["body"],
             status or item["status"], published_at, category if category is not None else item["category"],
             sources if sources is not None else item["sources"], news_id))
        self.conn.commit()

    def delete_news(self, news_id: int) -> None:
        self.conn.execute("DELETE FROM news WHERE id = ?", (news_id,))
        self.conn.commit()

    def news_to_post(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM news WHERE status = 'published' AND posted_at IS NULL "
                                 "ORDER BY published_at LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def mark_news_posted(self, news_id: int) -> None:
        self.conn.execute("UPDATE news SET posted_at = ? WHERE id = ?", (_now(), news_id))
        self.conn.commit()

    # -- «Предложить вуз» -----------------------------------------------------

    def add_uni_request(self, *, university: str, program: str = "", contact: str = "",
                        telegram_id: int | None = None) -> int:
        cur = self.conn.execute("INSERT INTO uni_request (telegram_id, university, program, contact, created_at) "
                                "VALUES (?, ?, ?, ?, ?)", (telegram_id, university[:200], program[:300],
                                                           contact[:200], _now()))
        self.conn.commit()
        return cur.lastrowid

    def recent_uni_request(self, telegram_id: int | None, university: str, hours: int = 24) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM uni_request WHERE lower(university) = lower(?) AND created_at >= ? "
            "AND coalesce(telegram_id, 0) = coalesce(?, 0)", (university, _ago(minutes=hours * 60), telegram_id))
        return row.fetchone() is not None

    def uni_requests_since(self, minutes: int) -> int:
        return self.conn.execute("SELECT count(*) FROM uni_request WHERE created_at >= ?",
                                 (_ago(minutes=minutes),)).fetchone()[0]

    def uni_requests(self, status: str = "new") -> list[dict]:
        rows = self.conn.execute("SELECT * FROM uni_request WHERE status = ? ORDER BY created_at DESC", (status,))
        return [dict(r) for r in rows]

    def close_uni_requests(self, university: str) -> None:
        self.conn.execute("UPDATE uni_request SET status = 'done' WHERE lower(university) = lower(?)", (university,))
        self.conn.commit()

    # -- аналитика ---------------------------------------------------------------

    def log_bot_user(self, telegram_id: int, username: str | None, first_name: str | None,
                     last_name: str | None, day: str) -> None:
        """Каждое действие в боте: обновляем @username/имя и считаем активность за день."""
        self.conn.execute(
            "INSERT INTO bot_user (telegram_id, created_at, username, first_name, last_name, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (telegram_id) DO UPDATE SET username = excluded.username, "
            "first_name = excluded.first_name, last_name = excluded.last_name, last_seen = excluded.last_seen",
            (telegram_id, _now(), username, first_name, last_name, _now()))
        self.conn.execute("INSERT INTO bot_activity (telegram_id, day, actions) VALUES (?, ?, 1) "
                          "ON CONFLICT (telegram_id, day) DO UPDATE SET actions = actions + 1", (telegram_id, day))
        self.conn.commit()

    def set_phone(self, telegram_id: int, phone: str | None) -> None:
        self.touch_user(telegram_id)
        self.conn.execute("UPDATE bot_user SET phone = ?, phone_at = ? WHERE telegram_id = ?",
                          (phone, _now() if phone else None, telegram_id))
        self.conn.commit()

    def user_info(self, telegram_id: int) -> dict:
        row = self.conn.execute("SELECT username, first_name, last_name, phone FROM bot_user WHERE telegram_id = ?",
                                (telegram_id,)).fetchone()
        return dict(row) if row else {"username": None, "first_name": None, "last_name": None, "phone": None}

    def log_pageview(self, *, visitor: str, telegram_id: int | None, path: str, ref_host: str, utm_source: str,
                     utm_campaign: str, device: str, day: str) -> None:
        self.conn.execute(
            "INSERT INTO pageview (ts, day, visitor, telegram_id, path, ref_host, utm_source, utm_campaign, device) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), day, visitor, telegram_id, path[:300], ref_host[:100], utm_source[:60], utm_campaign[:60], device))
        self.conn.execute(
            "INSERT INTO visitor (visitor, first_ts, ref_host, utm_source, utm_campaign, telegram_id) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (visitor) DO UPDATE SET "
            "telegram_id = coalesce(excluded.telegram_id, visitor.telegram_id)",
            (visitor, _now(), ref_host[:100], utm_source[:60], utm_campaign[:60], telegram_id))
        self.conn.commit()

    def log_search(self, source: str, query: str, results: int, telegram_id: int | None = None) -> None:
        self.conn.execute("INSERT INTO search_log (ts, source, query, results, telegram_id) VALUES (?, ?, ?, ?, ?)",
                          (_now(), source, query[:100], results, telegram_id))
        self.conn.commit()

    def log_auth(self, telegram_id: int, visitor: str = "") -> None:
        self.conn.execute("INSERT INTO auth_log (ts, telegram_id, visitor) VALUES (?, ?, ?)",
                          (_now(), telegram_id, visitor))
        if visitor:
            self.conn.execute("UPDATE visitor SET telegram_id = ? WHERE visitor = ?", (telegram_id, visitor))
        self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Для отчётов статистики (только SELECT из кода сайта)."""
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # -- этапы: управление из админки ------------------------------------------

    def delete_manual_stage(self, stage_id: int) -> bool:
        """Удалить этап, добавленный вручную (загруженные с сайта обновляются синхронизацией)."""
        cur = self.conn.execute("DELETE FROM stage WHERE id = ? AND external_id IS NULL", (stage_id,))
        self.conn.commit()
        return cur.rowcount == 1

    def stages_summary(self, today: date) -> dict:
        row = self.conn.execute(
            "SELECT count(*) AS total, count(DISTINCT olympiad_id) AS olympiads, "
            "sum(coalesce(ends_at, starts_at) >= ?) AS upcoming, sum(external_id IS NULL) AS manual FROM stage",
            (today.isoformat(),)).fetchone()
        return {k: row[k] or 0 for k in ("total", "olympiads", "upcoming", "manual")}
