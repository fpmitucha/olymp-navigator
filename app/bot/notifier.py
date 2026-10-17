"""Рассылка напоминаний об этапах. Не зависит от aiogram: отправка передаётся функцией.

Функция отправки должна бросать:
    UserBlocked     — пользователь заблокировал бота или удалил чат (больше не пишем ему);
    TemporaryError  — сеть или лимиты Telegram (повторим в следующий проход);
любая другая ошибка считается постоянной: напоминание помечается отправленным,
чтобы не слать его по кругу.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable

from app.bot import texts
from app.bot.storage import Storage
from app.catalog import Catalog
from app.stages import ANNOUNCE, due_announcements, due_reminders

log = logging.getLogger(__name__)

MOSCOW = timezone(timedelta(hours=3), "MSK")  # без перехода на летнее время с 2014 года
QUIET_BEFORE = 9    # не будим школьников: шлём с 9:00
QUIET_AFTER = 21    # до 21:00 по Москве
SEND_PAUSE = 0.05   # Telegram ограничивает ~30 сообщений в секунду


class UserBlocked(Exception):
    pass


class TemporaryError(Exception):
    pass


SendFn = Callable[[int, str], Awaitable[None]]


@dataclass
class RunResult:
    sent: int = 0
    blocked: int = 0
    failed: int = 0
    postponed: int = 0


def moscow_now() -> datetime:
    return datetime.now(MOSCOW)


def is_quiet_time(now: datetime) -> bool:
    hour = now.astimezone(MOSCOW).hour
    return not (QUIET_BEFORE <= hour < QUIET_AFTER)


STAGES_SYNC_EVERY = timedelta(hours=20)
STAGES_SYNC_KEY = "stages_synced_at"


async def stages_sync_loop(catalog: Catalog, db_path: str, fetch, check_every: float = 60 * 60) -> None:
    """Раз в сутки забирает даты этапов с olimpiada.ru. Синхронизация идёт в отдельном потоке
    со своим подключением к базе, чтобы бот продолжал отвечать."""
    from app.stages_sync import sync_stages

    def run_sync() -> str:
        storage = Storage(db_path)
        try:
            last = storage.get_meta(STAGES_SYNC_KEY)
            now = datetime.now(timezone.utc)
            if last and now - datetime.fromisoformat(last) < STAGES_SYNC_EVERY:
                return ""
            report = sync_stages(catalog, storage, fetch, moscow_now().date())
            storage.set_meta(STAGES_SYNC_KEY, now.isoformat(timespec="seconds"))
            return report.summary()
        finally:
            storage.close()

    while True:
        try:
            summary = await asyncio.to_thread(run_sync)
            if summary:
                log.info("Даты этапов обновлены с olimpiada.ru: %s", summary)
        except Exception:  # noqa: BLE001 — сайт может быть недоступен, попробуем через час
            log.exception("Не удалось обновить даты этапов с olimpiada.ru")
        await asyncio.sleep(check_every)


async def send_due_reminders(
    storage: Storage, catalog: Catalog, send: SendFn, today: date, pause: float = SEND_PAUSE
) -> RunResult:
    result = RunResult()
    # Олимпиаду могли переименовать: приводим старые id подписок и этапов к актуальным
    stages = [replace(s, olympiad_id=catalog.canonical_id(s.olympiad_id)) for s in storage.stages()]
    subscribers: dict[str, list[int]] = {}
    for olympiad_id, users in storage.subscribers_by_olympiad().items():
        bucket = subscribers.setdefault(catalog.canonical_id(olympiad_id), [])
        bucket.extend(u for u in users if u not in bucket)
    reminders = due_reminders(today, stages, subscribers, storage.sent_notifications())
    blocked_users: set[int] = set()
    for r in reminders:
        if r.telegram_id in blocked_users:
            continue
        olympiad = catalog.get(r.stage.olympiad_id)
        if olympiad is None:
            log.warning("Этап %s ссылается на неизвестную олимпиаду %s", r.stage.id, r.stage.olympiad_id)
            continue
        try:
            await send(r.telegram_id, texts.reminder(r, olympiad))
        except UserBlocked:
            storage.deactivate_user(r.telegram_id)
            blocked_users.add(r.telegram_id)
            result.blocked += 1
            continue
        except TemporaryError as exc:
            log.warning("Не удалось отправить напоминание %s: %s, повторю позже", r, exc)
            result.postponed += 1
            continue
        except Exception:  # noqa: BLE001 — постоянная ошибка, не повторяем
            log.exception("Ошибка при отправке напоминания %s", r)
            result.failed += 1
        else:
            result.sent += 1
        storage.log_notification(r.telegram_id, r.stage.id, r.kind)
        if pause:
            await asyncio.sleep(pause)
    return result


async def send_new_stage_announcements(
    storage: Storage, catalog: Catalog, send: SendFn, today: date, pause: float = SEND_PAUSE
) -> RunResult:
    """Подписчикам — одно сообщение на олимпиаду о появившихся датах."""
    result = RunResult()
    stages = [replace(s, olympiad_id=catalog.canonical_id(s.olympiad_id)) for s in storage.stages()]
    subs = [(tid, catalog.canonical_id(oid), since) for tid, oid, since in storage.subscriptions_since()]
    blocked: set[int] = set()
    for (telegram_id, olympiad_id), fresh in due_announcements(
            today, stages, subs, storage.sent_notifications()).items():
        olympiad = catalog.get(olympiad_id)
        if olympiad is None or telegram_id in blocked:
            continue
        try:
            await send(telegram_id, texts.new_stages(olympiad, fresh))
        except UserBlocked:
            storage.deactivate_user(telegram_id)
            blocked.add(telegram_id)
            result.blocked += 1
            continue
        except TemporaryError as exc:
            log.warning("Не удалось отправить новые даты %s: %s, повторю позже", olympiad_id, exc)
            result.postponed += 1
            continue
        except Exception:  # noqa: BLE001
            log.exception("Ошибка при отправке новых дат %s", olympiad_id)
            result.failed += 1
        else:
            result.sent += 1
        for s in fresh:
            storage.log_notification(telegram_id, s.id, ANNOUNCE)
        if pause:
            await asyncio.sleep(pause)
    return result


async def reminder_loop(storage: Storage, catalog: Catalog, send: SendFn, interval: float = 15 * 60) -> None:
    """Каждые 15 минут проверяет, не пора ли напомнить. Ночью молчит."""
    while True:
        now = moscow_now()
        if not is_quiet_time(now):
            try:
                result = await send_due_reminders(storage, catalog, send, now.date())
                if result.sent or result.blocked or result.failed or result.postponed:
                    log.info("Напоминания: %s", result)
                result = await send_new_stage_announcements(storage, catalog, send, now.date())
                if result.sent or result.blocked or result.failed or result.postponed:
                    log.info("Сообщения о новых датах: %s", result)
            except Exception:  # noqa: BLE001 — цикл не должен падать
                log.exception("Сбой рассылки напоминаний")
        await asyncio.sleep(interval)


BENEFITS_WATCH_EVERY = timedelta(hours=6)
BENEFITS_WATCH_KEY = "benefits_watched_at"


async def benefits_watch_loop(db_path: str, sources_path, admission_year: int, send: SendFn,
                              admin_id: int = 0, check_every: float = 30 * 60) -> None:
    """Раз в 6 часов (днём) проверяет страницы приёма вузов; находки — подписчикам вуза и администратору."""
    from app.benefits_watch import load_sources, run_watch
    from app.fetch import fetch_bytes, fetch_html

    def check():
        storage = Storage(db_path)
        try:
            last = storage.get_meta(BENEFITS_WATCH_KEY)
            now = datetime.now(timezone.utc)
            if last and now - datetime.fromisoformat(last) < BENEFITS_WATCH_EVERY:
                return None, {}
            report = run_watch(storage, load_sources(sources_path), admission_year, fetch_html, fetch_bytes)
            storage.set_meta(BENEFITS_WATCH_KEY, now.isoformat(timespec="seconds"))
            by_uni: dict[str, list] = {}
            for f in report.findings:
                by_uni.setdefault(f.university, []).append(f)
            return report, {u: (fs, storage.university_subscribers(u)) for u, fs in by_uni.items()}
        finally:
            storage.close()

    while True:
        if not is_quiet_time(moscow_now()):
            try:
                report, per_uni = await asyncio.to_thread(check)
                if report is not None:
                    if report.errors:
                        log.info("Слежение за вузами: не открылись %s", "; ".join(report.errors))
                    for university, (findings, users) in per_uni.items():
                        log.info("Слежение за вузами: %s — %s", university, [f.url for f in findings])
                        text = texts.benefits_published(university, findings, admission_year)
                        for telegram_id in dict.fromkeys(([admin_id] if admin_id else []) + users):
                            try:
                                await send(telegram_id, text)
                            except Exception:  # noqa: BLE001 — одному не дошло, остальным отправим
                                log.exception("Не отправилось сообщение о документах %s пользователю %s",
                                              university, telegram_id)
                            await asyncio.sleep(SEND_PAUSE)
            except Exception:  # noqa: BLE001
                log.exception("Сбой слежения за вузами")
        await asyncio.sleep(check_every)


BENEFITS_SNAPSHOT_PREFIX = "benefits_snapshot:"


def benefits_updates(storage: Storage, benefits) -> list[tuple]:
    """Вузы, чьи льготы изменились с прошлого запуска бота: (вуз, прошлая подпись, прошлый снимок).
    При первом запуске только запоминаем — сообщать не о чем."""
    from app.benefits_diff import dump_snapshot

    changed = []
    for u in benefits.latest():
        key = BENEFITS_SNAPSHOT_PREFIX + u.university
        label = f"{u.admission_year}:{u.document_status}"
        current = dump_snapshot(u.rules)
        stored = storage.get_meta(key)
        if stored is None:
            storage.set_meta(key, json.dumps({"label": label, "snapshot": current}, ensure_ascii=False))
            continue
        previous = json.loads(stored)
        if previous["snapshot"] != current or previous["label"] != label:
            changed.append((u, previous["label"], previous["snapshot"]))
    return changed


async def send_benefits_updates(storage: Storage, catalog: Catalog, benefits, send: SendFn,
                                admin_id: int = 0, pause: float = SEND_PAUSE) -> int:
    """Подписчикам вуза — что изменилось в льготах (по их олимпиадам — в первую очередь)."""
    from app.benefits_diff import diff, load_snapshot, snapshot

    sent = 0
    for u, old_label, old_snapshot in benefits_updates(storage, benefits):
        old_rows, names = load_snapshot(old_snapshot)
        names.update({r.olympiad.id: r.olympiad.name for r in u.rules})
        changes = diff(old_rows, snapshot(u.rules), names)
        users = storage.university_subscribers(u.university)
        for telegram_id in dict.fromkeys(([admin_id] if admin_id else []) + users):
            focus = {catalog.canonical_id(o) for o in storage.subscriptions(telegram_id)}
            text = texts.benefits_changed(u, old_label, changes, focus)
            try:
                await send(telegram_id, text)
                sent += 1
            except UserBlocked:
                storage.deactivate_user(telegram_id)
            except Exception:  # noqa: BLE001
                log.exception("Не отправилось сообщение об изменениях льгот %s пользователю %s",
                              u.university, telegram_id)
            if pause:
                await asyncio.sleep(pause)
        from app.benefits_diff import dump_snapshot

        storage.set_meta(BENEFITS_SNAPSHOT_PREFIX + u.university, json.dumps(
            {"label": f"{u.admission_year}:{u.document_status}", "snapshot": dump_snapshot(u.rules)},
            ensure_ascii=False))
        log.info("Льготы %s изменились (%s изменений) — сообщено %s подписчикам", u.university, len(changes),
                 len(users))
    return sent


async def benefits_updates_loop(storage: Storage, catalog: Catalog, benefits, send: SendFn,
                                admin_id: int = 0, interval: float = 15 * 60) -> None:
    """При запуске бота сравнивает льготы с прошлым запуском; ночью ждёт утра."""
    while is_quiet_time(moscow_now()):
        await asyncio.sleep(interval)
    try:
        await send_benefits_updates(storage, catalog, benefits, send, admin_id)
    except Exception:  # noqa: BLE001
        log.exception("Сбой рассылки об изменениях льгот")


# -- заявки на сопровождение ------------------------------------------------------

def about_user(storage: Storage, catalog: Catalog, telegram_id: int | None) -> list[str]:
    """Что известно о человеке из «Моего пути»: класс, дипломы, вузы-цели, олимпиады."""
    if not telegram_id:
        return []
    lines = []
    profile = storage.profile(telegram_id)
    if profile.get("grade"):
        lines.append(f"Класс (в кабинете): {profile['grade']}")
    diplomas = storage.diplomas(telegram_id)
    if diplomas:
        lines.append("<b>Дипломы:</b>")
        for d in diplomas:
            o = catalog.get(d["olympiad_id"])
            name = texts.short_name(o.name, 50) if o else d["olympiad_id"]
            lines.append(f"• {texts.escape(name)}, {texts.escape(d['profile'])} — "
                         f"{texts.DIPLOMA_TITLES.get(d['diploma'], d['diploma'])}, {d['academic_year']}")
    targets = storage.university_subscriptions(telegram_id)
    if targets:
        lines.append("<b>Вузы-цели:</b> " + texts.escape(", ".join(targets)))
    olympiads = [catalog.get(x) for x in storage.subscriptions(telegram_id)]
    names = [texts.short_name(o.name, 40) for o in olympiads if o]
    if names:
        lines.append("<b>Следит за:</b> " + texts.escape(", ".join(names[:10]))
                     + (f" и ещё {len(names) - 10}" if len(names) > 10 else ""))
    return lines


async def forward_help_requests(storage: Storage, catalog: Catalog, send: SendFn, admin_id: int) -> int:
    """Пересылает администратору новые заявки (с сайта и из бота). Возвращает число пересланных."""
    if not admin_id:
        return 0
    sent = 0
    for req in storage.pending_help_requests():
        try:
            await send(admin_id, texts.help_request_admin(req, about_user(storage, catalog, req["telegram_id"])))
            sent += 1
        except TemporaryError:
            break  # сеть — попробуем в следующий проход
        except Exception:  # noqa: BLE001 — сломанная заявка не должна блокировать остальные
            log.exception("Не переслалась заявка на сопровождение #%s", req["id"])
        storage.mark_help_forwarded(req["id"])
        await asyncio.sleep(SEND_PAUSE)
    return sent


async def help_requests_loop(db_path: str, catalog: Catalog, send: SendFn, admin_id: int,
                             interval: float = 60) -> None:
    """Заявки с сайта попадают в общую базу — раз в минуту пересылаем новые администратору."""
    if not admin_id:
        log.warning("BOT_ADMIN_ID не задан — заявки на сопровождение и сообщения об ошибках копятся в базе")
        return
    while True:
        storage = Storage(db_path)
        try:
            await forward_help_requests(storage, catalog, send, admin_id)
        except Exception:  # noqa: BLE001
            log.exception("Сбой пересылки заявок на сопровождение")
        finally:
            storage.close()
        await asyncio.sleep(interval)
