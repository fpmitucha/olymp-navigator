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
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, SendMessage
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
            self.edit_error: str | None = None

        async def make_request(self, bot, method, timeout=None):
            self.calls.append(method)
            if isinstance(method, SendMessage):
                self._next_id += 1
                return Message(
                    message_id=self._next_id, date=datetime.now(), text=method.text,
                    chat=Chat(id=method.chat_id, type="private"), reply_markup=method.reply_markup,
                )
            if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText)):
                if self.edit_error and isinstance(method, EditMessageText):
                    raise TelegramBadRequest(method=method, message=self.edit_error)
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

    @staticmethod
    def nav(**kwargs) -> str:
        from app.bot.main import Nav

        return Nav(**kwargs).pack()

    def edited(self, calls):
        return [c for c in calls if isinstance(c, EditMessageText)]

    def only_edit(self, calls):
        """Переход по кнопке должен менять сообщение, а не присылать новое."""
        self.assertEqual(self.sent(calls), [])
        edits = self.edited(calls)
        self.assertEqual(len(edits), 1)
        return edits[0]

    def button(self, call, text_start: str) -> str:
        for label, data in self.buttons(call):
            if label.startswith(text_start):
                return data
        raise AssertionError(f"Нет кнопки «{text_start}…» среди {self.buttons(call)}")

    # -- tests -----------------------------------------------------------------

    def test_start_shows_menu(self):
        calls = self.sent(self.feed(self.message("/start")))
        self.assertEqual(len(calls), 1)
        self.assertIn("перечня РСОШ", calls[0].text)
        labels = [label for label, _ in self.buttons(calls[0])]
        self.assertEqual(labels[:2], ["🔍 Физтех", "🔍 ВсОШ"])
        self.assertIn("🔔 Мои подписки", labels)
        self.assertEqual(self.storage.stats()["users"], 1)

    def test_menu_navigation_edits_message(self):
        help_ = self.only_edit(self.feed(self.callback(self.nav(a="help"))))
        self.assertIn("Как пользоваться", help_.text)
        menu = self.only_edit(self.feed(self.callback(self.button(help_, "🏠"))))
        self.assertIn("Привет", menu.text)
        example = self.only_edit(self.feed(self.callback(self.button(menu, "🔍 Физтех"))))
        self.assertIn("<b>Физтех</b>", example.text)

    def test_unknown_command(self):
        self.assertIn("Не знаю такой команды", self.sent(self.feed(self.message("/foo")))[0].text)

    def test_single_hit_opens_card(self):
        calls = self.sent(self.feed(self.message("Физтех")))
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].text.startswith("<b>Физтех</b>"))
        labels = [label for label, _ in self.buttons(calls[0])]
        self.assertEqual(labels, ["🔔 Подписаться на этапы", "📋 Подробнее", "🌐 Сайт", "🏠 Меню"])

    def test_results_card_and_back(self):
        results = self.sent(self.feed(self.message("физика")))[0]
        self.assertIn("🔍 <b>физика</b>", results.text)
        self.assertEqual(self.buttons(results)[0][0], "ВсОШ · физика · БВИ")
        self.assertTrue(all(len(data.encode()) <= 64 for _, data in self.buttons(results) if data))

        card = self.only_edit(self.feed(self.callback(self.button(results, "Физтех"))))
        self.assertTrue(card.text.startswith("<b>Физтех</b>"))

        details = self.only_edit(self.feed(self.callback(self.button(card, "📋 Подробнее"))))
        self.assertIn("Профили и уровни по годам", details.text)
        brief = self.only_edit(self.feed(self.callback(self.button(details, "📋 Кратко"))))
        self.assertIn("1 уровень", brief.text)

        back = self.only_edit(self.feed(self.callback(self.button(brief, "◀️ К результатам"))))
        self.assertEqual(back.text, results.text)

    def test_pagination(self):
        import app.bot.main as bot_main

        old = bot_main.PAGE_SIZE
        bot_main.PAGE_SIZE = 2
        self.addCleanup(setattr, bot_main, "PAGE_SIZE", old)
        first = self.sent(self.feed(self.message("физика")))[0]
        self.assertIn("страница 1 из", first.text)
        labels = [label for label, _ in self.buttons(first)]
        self.assertIn("·", labels)  # на первой странице нет «назад»
        second = self.only_edit(self.feed(self.callback(self.button(first, "▶️"))))
        self.assertIn("страница 2 из", second.text)
        self.assertNotEqual(self.buttons(second)[0], self.buttons(first)[0])
        noop = self.feed(self.callback(self.button(second, "2 /")))
        self.assertEqual(self.edited(noop), [])

    def test_not_found(self):
        calls = self.sent(self.feed(self.message("абракадабра")))
        self.assertIn("ничего не нашлось", calls[0].text)
        self.assertEqual([label for label, _ in self.buttons(calls[0])], ["🏠 Меню"])

    def test_subscribe_keeps_card_state(self):
        fiztech = self.catalog.search("физтех")[0].olympiad
        self.storage.upsert_stages([
            StageRow(fiztech.id, "final", "Заключительный этап", date(2027, 2, 20), None, None, "2026/27", None),
        ])
        card = self.only_edit(self.feed(self.callback(self.nav(a="card", o=fiztech.id, d=1))))
        self.assertIn("20 февраля — Заключительный этап", card.text)

        calls = self.feed(self.callback(self.button(card, "🔔 Подписаться")))
        self.assertEqual(self.sent(calls), [])
        edit = next(c for c in calls if isinstance(c, EditMessageReplyMarkup))
        rows = [[b.text for b in row] for row in edit.reply_markup.inline_keyboard]
        self.assertEqual(rows[0], ["🔕 Отписаться"])
        self.assertEqual(rows[1][0], "📋 Кратко")  # подробный вид сохранился
        answer = next(c for c in calls if isinstance(c, AnswerCallbackQuery))
        self.assertIn("Напомню", answer.text)
        self.assertEqual(self.storage.subscriptions(USER_ID), [fiztech.id])

        subs = self.only_edit(self.feed(self.callback(self.nav(a="subs"))))
        self.assertIn("Подписки (1)", subs.text)
        from_subs = self.only_edit(self.feed(self.callback(self.button(subs, "Физтех"))))
        self.assertIn("◀️ К подпискам", [label for label, _ in self.buttons(from_subs)])

        self.feed(self.callback(self.nav(a="unsub", o=fiztech.id)))
        self.assertEqual(self.storage.subscriptions(USER_ID), [])

    def test_unsubscribe_old_id_after_rename(self):
        from app.catalog import short_id
        from app.parsers.rsr import olympiad_key

        old_id = short_id(olympiad_key("Объединенная межвузовская математическая олимпиада школьников"))
        self.storage.subscribe(USER_ID, old_id)
        ommo = self.catalog.get(old_id)
        card = self.only_edit(self.feed(self.callback(self.nav(a="card", o=ommo.id))))
        self.assertEqual(self.buttons(card)[0][0], "🔕 Отписаться")
        self.feed(self.callback(self.nav(a="unsub", o=ommo.id)))
        self.assertEqual(self.storage.subscriptions(USER_ID), [])

    def test_unknown_olympiad_and_stale_buttons(self):
        cases = (self.nav(a="card", o="deadbeef00"), self.nav(a="page", q=9999), "o:open:abc", "n:help")
        for data in cases:
            calls = self.feed(self.callback(data))
            answer = next(c for c in calls if isinstance(c, AnswerCallbackQuery))
            self.assertTrue(answer.show_alert, data)
            self.assertEqual(self.edited(calls), [], data)

    def test_edit_fallbacks(self):
        self.session.edit_error = "Bad Request: message is not modified"
        calls = self.feed(self.callback(self.nav(a="help")))
        self.assertEqual(self.sent(calls), [])
        self.session.edit_error = "Bad Request: message can't be edited"
        calls = self.feed(self.callback(self.nav(a="help")))
        self.assertIn("Как пользоваться", self.sent(calls)[0].text)

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
