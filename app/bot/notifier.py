"""Рассылка напоминаний об этапах. Не зависит от aiogram: отправка передаётся функцией.

Функция отправки должна бросать:
    UserBlocked     — пользователь заблокировал бота или удалил чат (больше не пишем ему);
    TemporaryError  — сеть или лимиты Telegram (повторим в следующий проход);
любая другая ошибка считается постоянной: напоминание помечается отправленным,
чтобы не слать его по кругу.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable

from app.bot import texts
from app.bot.storage import Storage
from app.catalog import Catalog
from app.stages import due_reminders

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


async def reminder_loop(storage: Storage, catalog: Catalog, send: SendFn, interval: float = 15 * 60) -> None:
    """Каждые 15 минут проверяет, не пора ли напомнить. Ночью молчит."""
    while True:
        now = moscow_now()
        if not is_quiet_time(now):
            try:
                result = await send_due_reminders(storage, catalog, send, now.date())
                if result.sent or result.blocked or result.failed or result.postponed:
                    log.info("Напоминания: %s", result)
            except Exception:  # noqa: BLE001 — цикл не должен падать
                log.exception("Сбой рассылки напоминаний")
        await asyncio.sleep(interval)
