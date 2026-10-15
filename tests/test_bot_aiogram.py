"""Сквозные тесты бота: настоящий Dispatcher aiogram, поддельный Telegram.

Запросы к Telegram перехватывает FakeSession, поэтому токен и сеть не нужны.
Если aiogram не установлен, тесты пропускаются.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime

try:
    from aiogram import Bot
    from aiogram.client.session.base import BaseSession
    from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    HAS_AIOGRAM = True
except ImportError:  # pragma: no cover
    HAS_AIOGRAM = False

from app.bot.storage import Storage
from app.stages import StageRow
from tests.helpers import sample_catalog

TODAY = date(2026, 11, 10)
USER_ID = 555


if HAS_AIOGRAM:
    class FakeSession(BaseSession):
        """Записывает вызовы методов Telegram и возвращает правдоподобные ответы."""

        def __init__(self):
            super().__init__()
            self.calls: list = []
            self._next_id = 100

        async def make_request(self, bot, method, timeout=None):
            self.calls.append(method)
            if isinstance(method, SendMessage):
                self._next_id += 1
                return Message(
                    message_id=self._next_id, date=datetime.now(), text=method.text,
                    chat=Chat(id=method.chat_id, type="private"), reply_markup=method.reply_markup,
                )
            if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
                return True
            raise AssertionError(f"Неожиданный вызов {type(method).__name__}")

        async def stream_content(self, *args, **kwargs):  # pragma: no cover
            raise NotImplementedError

        async def close(self):
            pass


@unittest.skipUnless(HAS_AIOGRAM, "aiogram не установлен")
class BotFlowTest(unittest.TestCase):
    def setUp(self):
        from app.bot.main import build_dispatcher

        self.catalog = sample_catalog()
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)
        self.session = FakeSession()
        self.bot = Bot("42:TEST", session=self.session)
        self.dp = build_dispatcher(self.catalog, self.storage, today=lambda: TODAY)
        self.update_id = 0

    # -- helpers ---------------------------------------------------------------

    def user(self):
        return User(id=USER_ID, is_bot=False, first_name="Тест")

    def message(self, text: str):
        self.update_id += 1
        msg = Message(message_id=self.update_id, date=datetime.now(), text=text,
                      chat=Chat(id=USER_ID, type="private"), from_user=self.user())
        return Update(update_id=self.update_id, message=msg)

    def callback(self, data: str):
        self.update_id += 1
        msg = Message(message_id=1, date=datetime.now(), text="карточка", chat=Chat(id=USER_ID, type="private"))
        cq = CallbackQuery(id=str(self.update_id), from_user=self.user(), chat_instance="1", message=msg, data=data)
        return Update(update_id=self.update_id, callback_query=cq)

    def feed(self, update):
        self.session.calls.clear()
        asyncio.run(self.dp.feed_update(self.bot, update))
        return list(self.session.calls)

    def sent(self, calls):
        return [c for c in calls if isinstance(c, SendMessage)]

    def buttons(self, call):
        markup = call.reply_markup
        return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row] if markup else []

    # -- tests -----------------------------------------------------------------

    def test_start_registers_user(self):
        calls = self.sent(self.feed(self.message("/start")))
        self.assertEqual(len(calls), 1)
        self.assertIn("перечня РСОШ", calls[0].text)
        self.assertEqual(self.storage.stats()["users"], 1)

    def test_help_and_unknown_command(self):
        self.assertIn("Как пользоваться", self.sent(self.feed(self.message("/help")))[0].text)
        self.assertIn("Не знаю такой команды", self.sent(self.feed(self.message("/foo")))[0].text)

    def test_single_hit_opens_card(self):
        calls = self.sent(self.feed(self.message("Физтех")))
        self.assertEqual(len(calls), 1)
        self.assertIn("<b>Олимпиада школьников «Физтех»</b>", calls[0].text)
        self.assertEqual(self.buttons(calls[0])[0][0], "🔔 Подписаться на этапы")

    def test_many_hits_show_list(self):
        calls = self.sent(self.feed(self.message("физика")))
        self.assertIn("нашлось", calls[0].text)
        labels = [label for label, _ in self.buttons(calls[0])]
        self.assertEqual(labels[0], "ВсОШ · физика · БВИ")
        self.assertTrue(all(len(data.encode()) <= 64 for _, data in self.buttons(calls[0])))

    def test_not_found(self):
        calls = self.sent(self.feed(self.message("абракадабра")))
        self.assertIn("Ничего не нашлось", calls[0].text)
        self.assertIsNone(calls[0].reply_markup)

    def test_subscribe_flow_and_card_shows_stage(self):
        fiztech = self.catalog.search("физтех")[0].olympiad
        self.storage.upsert_stages([
            StageRow(fiztech.id, "final", "Заключительный этап", date(2027, 2, 20), None, None, "2026/27", None),
        ])
        open_calls = self.sent(self.feed(self.callback(f"o:open:{fiztech.id}")))
        self.assertIn("20 февраля — Заключительный этап", open_calls[0].text)

        calls = self.feed(self.callback(f"o:sub:{fiztech.id}"))
        edit = next(c for c in calls if isinstance(c, EditMessageReplyMarkup))
        self.assertEqual(edit.reply_markup.inline_keyboard[0][0].text, "🔕 Отписаться")
        answer = next(c for c in calls if isinstance(c, AnswerCallbackQuery))
        self.assertIn("Напомню", answer.text)
        self.assertEqual(self.storage.subscriptions(USER_ID), [fiztech.id])

        subs = self.sent(self.feed(self.message("/subs")))
        self.assertIn("Твои подписки (1)", subs[0].text)
        self.assertEqual(self.buttons(subs[0])[0], ("Физтех", f"o:open:{fiztech.id}"))

        self.feed(self.callback(f"o:unsub:{fiztech.id}"))
        self.assertEqual(self.storage.subscriptions(USER_ID), [])

    def test_unsubscribe_old_id_after_rename(self):
        from app.catalog import short_id
        from app.parsers.rsr import olympiad_key

        old_id = short_id(olympiad_key("Объединенная межвузовская математическая олимпиада школьников"))
        self.storage.subscribe(USER_ID, old_id)
        ommo = self.catalog.get(old_id)
        calls = self.sent(self.feed(self.callback(f"o:open:{ommo.id}")))
        self.assertEqual(self.buttons(calls[0])[0][0], "🔕 Отписаться")
        self.feed(self.callback(f"o:unsub:{ommo.id}"))
        self.assertEqual(self.storage.subscriptions(USER_ID), [])

    def test_unknown_olympiad_id(self):
        calls = self.feed(self.callback("o:open:deadbeef00"))
        answer = next(c for c in calls if isinstance(c, AnswerCallbackQuery))
        self.assertTrue(answer.show_alert)

    def test_sender_translates_errors(self):
        from aiogram.exceptions import TelegramForbiddenError

        from app.bot.main import make_sender
        from app.bot.notifier import UserBlocked

        class Blocking(FakeSession):
            async def make_request(self, bot, method, timeout=None):
                raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")

        send = make_sender(Bot("42:TEST", session=Blocking()))
        with self.assertRaises(UserBlocked):
            asyncio.run(send(1, "привет"))


if __name__ == "__main__":
    unittest.main()
