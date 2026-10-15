"""Хранилище, тексты и рассылка напоминаний — без aiogram."""

from __future__ import annotations

import asyncio
import unittest
from datetime import date

from app.bot import texts
from app.bot.notifier import TemporaryError, UserBlocked, is_quiet_time, send_due_reminders
from app.bot.storage import Storage
from app.catalog import short_id
from app.parsers.rsr import olympiad_key
from app.stages import Reminder, Stage, StageRow
from tests.helpers import sample_catalog

TODAY = date(2026, 11, 10)


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.s = Storage(":memory:")
        self.addCleanup(self.s.close)

    def test_subscriptions(self):
        self.assertTrue(self.s.subscribe(1, "a"))
        self.assertFalse(self.s.subscribe(1, "a"))
        self.s.subscribe(1, "b")
        self.s.subscribe(2, "a")
        self.assertEqual(self.s.subscriptions(1), ["a", "b"])
        self.assertTrue(self.s.is_subscribed(2, "a"))
        self.assertEqual(self.s.subscribers_by_olympiad(), {"a": [1, 2], "b": [1]})
        self.s.deactivate_user(2)
        self.assertEqual(self.s.subscribers_by_olympiad(), {"a": [1], "b": [1]})
        self.s.touch_user(2)  # вернулся, нажал /start
        self.assertEqual(self.s.subscribers_by_olympiad()["a"], [1, 2])
        self.assertTrue(self.s.unsubscribe(1, "a"))
        self.assertFalse(self.s.unsubscribe(1, "a"))
        self.assertEqual(self.s.stats(), {"users": 2, "subscriptions": 2, "stages": 0})

    def test_stage_upsert_is_idempotent(self):
        row = StageRow("o1", "final", "Финал", date(2027, 2, 20), None, None, "2026/27", None)
        self.s.upsert_stages([row])
        moved = StageRow("o1", "final", "Финал", date(2027, 2, 22), date(2027, 2, 23), None, "2026/27", "https://x")
        self.s.upsert_stages([moved])
        stages = self.s.stages("o1")
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0].starts_at, date(2027, 2, 22))
        self.assertIsNone(stages[0].profile)
        self.assertEqual(self.s.upcoming_stages("o1", date(2027, 2, 23))[0].id, stages[0].id)
        self.assertEqual(self.s.upcoming_stages("o1", date(2027, 2, 24)), [])

    def test_notification_log(self):
        self.s.log_notification(1, 5, "stage_t7")
        self.s.log_notification(1, 5, "stage_t7")
        self.assertEqual(self.s.sent_notifications(), {(1, 5, "stage_t7")})


class TextsTest(unittest.TestCase):
    def test_days_phrase(self):
        self.assertEqual(texts.days_phrase(0), "сегодня")
        self.assertEqual(texts.days_phrase(1), "завтра")
        self.assertEqual(texts.days_phrase(2), "через 2 дня")
        self.assertEqual(texts.days_phrase(5), "через 5 дней")
        self.assertEqual(texts.days_phrase(21), "через 21 день")
        self.assertEqual(texts.days_phrase(11), "через 11 дней")

    def test_periods(self):
        self.assertEqual(texts.ru_period(date(2026, 11, 20), None), "20 ноября")
        self.assertEqual(texts.ru_period(date(2026, 11, 20), date(2026, 11, 22)), "20–22 ноября")
        self.assertEqual(texts.ru_period(date(2026, 11, 30), date(2026, 12, 2)), "30 ноября – 2 декабря")
        self.assertEqual(texts.ru_period(None, None), "дата уточняется")

    def test_short_name(self):
        self.assertEqual(texts.short_name("Олимпиада школьников «Физтех»"), "Физтех")
        self.assertEqual(
            texts.short_name("Олимпиада ... «Всероссийский государственный университет юстиции (РПА)» «В мир права»"),
            "В мир права",
        )
        self.assertEqual(texts.short_name("Олимпиада школьников Санкт-Петербургского государственного университета"),
                         "Олимпиада СПбГУ")
        self.assertEqual(texts.short_name("Межрегиональная олимпиада школьников имени В.Е. Татлина"),
                         "Олимпиада им. В.Е. Татлина")
        self.assertTrue(texts.short_name("а" * 100).endswith("…"))

    def test_card(self):
        c = sample_catalog()
        fiztech = c.search("физтех")[0].olympiad
        stage = Stage(1, fiztech.id, "final", "Заключительный этап", date(2027, 2, 20), None)
        card = texts.olympiad_card(fiztech, c.current_year, [stage], TODAY)
        self.assertIn("математика — <b>1 ур.</b>", card)
        self.assertIn("прошлые годы: 25/26 — 2, 24/25 — 2", card)
        self.assertNotIn("прошлые годы: 25/26 — 1", card)  # у физики уровень не менялся
        self.assertIn("20 февраля — Заключительный этап", card)
        self.assertIn('href="https://olymp.mipt.ru"', card)

    def test_card_for_dropped_olympiad(self):
        c = sample_catalog()
        robofest = c.search("робофест")[0].olympiad
        card = texts.olympiad_card(robofest, c.current_year, [], TODAY)
        self.assertIn("нет в перечне 2026/27", card)
        self.assertIn("физика: 25/26 — 3, 24/25 — 3", card)

    def test_card_escapes_html_and_fits(self):
        c = sample_catalog()
        o = c.search("физтех")[0].olympiad
        o.name = "<script>"
        card = texts.olympiad_card(o, c.current_year, [], TODAY)
        self.assertIn("&lt;script&gt;", card)
        long_lines = ["<b>x</b>"] + [f"• профиль {i} — <b>1 ур.</b>" for i in range(400)]
        fitted = texts._fit(long_lines)
        self.assertLessEqual(len(fitted), texts.MAX_MESSAGE)
        self.assertIn("…и ещё", fitted)

    def test_vsosh_card(self):
        c = sample_catalog()
        card = texts.olympiad_card(c.search("всош")[0].olympiad, c.current_year, [], TODAY)
        self.assertIn("БВИ", card)
        self.assertIn("Предметы (24)", card)

    def test_reminder_texts(self):
        c = sample_catalog()
        o = c.search("физтех")[0].olympiad
        reg = Stage(1, o.id, "registration", "Регистрация", date(2026, 10, 1), date(2026, 11, 13), source_url="https://x.ru/?a=1&b=2")
        text = texts.reminder(Reminder(1, reg, "deadline_t3", 3), o)
        self.assertIn("Регистрация на «Физтех» закрывается через 3 дня, 13 ноября.", text)
        self.assertIn('href="https://x.ru/?a=1&amp;b=2"', text)
        final = Stage(2, o.id, "final", "Заключительный этап", date(2027, 2, 20), date(2027, 2, 21), profile="физика")
        text = texts.reminder(Reminder(1, final, "stage_t1", 1), o)
        self.assertEqual(text, "⏰ Завтра: «Физтех» (физика) — Заключительный этап, 20–21 февраля.")


class NotifierTest(unittest.TestCase):
    def setUp(self):
        self.catalog = sample_catalog()
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)
        self.fiztech = self.catalog.search("физтех")[0].olympiad
        self.storage.upsert_stages([
            StageRow(self.fiztech.id, "qualifying", "Отборочный этап", date(2026, 11, 11), None, None, "2026/27", None),
        ])

    def run_send(self, send):
        return asyncio.run(send_due_reminders(self.storage, self.catalog, send, TODAY, pause=0))

    def test_sends_once(self):
        self.storage.subscribe(1, self.fiztech.id)
        self.storage.subscribe(2, self.fiztech.id)
        sent = []

        async def send(uid, text):
            sent.append((uid, text))

        result = self.run_send(send)
        self.assertEqual(result.sent, 2)
        self.assertEqual({uid for uid, _ in sent}, {1, 2})
        self.assertIn("Завтра", sent[0][1])
        self.assertEqual(self.run_send(send).sent, 0)  # повторный проход ничего не шлёт

    def test_blocked_temporary_and_failed(self):
        for uid in (1, 2, 3):
            self.storage.subscribe(uid, self.fiztech.id)

        async def send(uid, text):
            if uid == 1:
                raise UserBlocked("blocked")
            if uid == 2:
                raise TemporaryError("network")
            raise RuntimeError("bad request")

        with self.assertLogs("app.bot.notifier", level="WARNING"):
            result = self.run_send(send)
        self.assertEqual((result.sent, result.blocked, result.postponed, result.failed), (0, 1, 1, 1))
        self.assertNotIn(1, self.storage.subscribers_by_olympiad()[self.fiztech.id])
        # временная ошибка — повторим, постоянная — нет
        self.assertEqual({k[0] for k in self.storage.sent_notifications()}, {3})

    def test_old_ids_after_rename(self):
        old_id = short_id(olympiad_key("Объединенная межвузовская математическая олимпиада школьников"))
        ommo = self.catalog.get(old_id)
        self.assertNotEqual(old_id, ommo.id)
        self.storage.subscribe(7, old_id)  # подписался, пока олимпиада называлась по-старому
        self.storage.upsert_stages([
            StageRow(ommo.id, "final", "Финал", TODAY, None, None, "2026/27", None),
        ])
        sent = []

        async def send(uid, text):
            sent.append(uid)

        self.run_send(send)
        self.assertEqual(sent, [7])

    def test_quiet_hours(self):
        from datetime import datetime

        from app.bot.notifier import MOSCOW

        self.assertTrue(is_quiet_time(datetime(2026, 11, 10, 8, 59, tzinfo=MOSCOW)))
        self.assertFalse(is_quiet_time(datetime(2026, 11, 10, 9, 0, tzinfo=MOSCOW)))
        self.assertTrue(is_quiet_time(datetime(2026, 11, 10, 21, 0, tzinfo=MOSCOW)))


if __name__ == "__main__":
    unittest.main()
