"""Telegram-бот на aiogram 3.

Навигация — инлайн-кнопками в одном сообщении: меню → результаты поиска → карточка
олимпиады → «Назад». Новое сообщение бот отправляет только в ответ на сообщение
пользователя (поисковый запрос или команду).

Запуск: python -m app.cli bot run  (токен берётся из TELEGRAM_BOT_TOKEN в .env)
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import date
from typing import Callable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.notifier import TemporaryError, UserBlocked, moscow_now, reminder_loop
from app.bot.storage import Storage
from app.catalog import Catalog, CatalogOlympiad

log = logging.getLogger(__name__)

PAGE_SIZE = 8
MAX_RESULTS = 200
MAX_QUERY = 100
EXAMPLES = ["Физтех", "ВсОШ", "информатика", "НТО"]


class Nav(CallbackData, prefix="n"):
    """Данные кнопок. Вся навигация — через этот класс (лимит Telegram 64 байта).

    a — действие: menu, help, subs, ex (пример поиска), page, card, sub, unsub, noop
    o — id олимпиады; q — id сохранённого запроса; p — страница результатов
    b — куда ведёт «Назад» из карточки: "" — меню, "s" — результаты поиска, "u" — подписки
    d — 1, если карточка открыта в подробном виде
    """

    a: str
    o: str = ""
    q: int = 0
    p: int = 0
    b: str = ""
    d: int = 0


Screen = tuple[str, InlineKeyboardMarkup]


class Views:
    """Экраны бота: текст + кнопки. Не отправляют сообщения сами."""

    def __init__(self, catalog: Catalog, storage: Storage, today: Callable[[], date]):
        self.catalog = catalog
        self.storage = storage
        self.today = today

    # -- подписки -------------------------------------------------------------

    def subscribed_ids(self, telegram_id: int, olympiad_id: str) -> list[str]:
        """Id, под которыми пользователь подписан на олимпиаду (включая старые до переименования)."""
        return [sid for sid in self.storage.subscriptions(telegram_id)
                if self.catalog.canonical_id(sid) == olympiad_id]

    def user_subscriptions(self, telegram_id: int) -> list[CatalogOlympiad]:
        result: list[CatalogOlympiad] = []
        for sid in self.storage.subscriptions(telegram_id):
            o = self.catalog.get(sid)
            if o and o not in result:
                result.append(o)
        return result

    # -- экраны ---------------------------------------------------------------

    def menu(self) -> Screen:
        kb = InlineKeyboardBuilder()
        for i, example in enumerate(EXAMPLES):
            kb.button(text=f"🔍 {example}", callback_data=Nav(a="ex", p=i))
        kb.button(text="🔔 Мои подписки", callback_data=Nav(a="subs"))
        kb.button(text="ℹ️ Как пользоваться", callback_data=Nav(a="help"))
        kb.adjust(2, 2, 1, 1)
        return texts.START, kb.as_markup()

    def help(self) -> Screen:
        kb = InlineKeyboardBuilder()
        kb.button(text="🏠 Меню", callback_data=Nav(a="menu"))
        return texts.HELP, kb.as_markup()

    def subs(self, telegram_id: int) -> Screen:
        olympiads = self.user_subscriptions(telegram_id)
        kb = InlineKeyboardBuilder()
        for o in olympiads:
            kb.button(text=texts.button_name(o), callback_data=Nav(a="card", o=o.id, b="u"))
        kb.button(text="🏠 Меню", callback_data=Nav(a="menu"))
        kb.adjust(1)
        return texts.subscriptions_list(olympiads), kb.as_markup()

    def results(self, query_id: int, page: int) -> Screen | None:
        query = self.storage.get_query(query_id)
        if query is None:
            return None
        hits = self.catalog.search(query, limit=MAX_RESULTS)
        pages = max(1, math.ceil(len(hits) / PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        kb = InlineKeyboardBuilder()
        for hit in hits[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            label = texts.hit_button_label(hit, self.catalog.current_year)
            kb.row(InlineKeyboardButton(
                text=label, callback_data=Nav(a="card", o=hit.olympiad.id, q=query_id, p=page, b="s").pack()))
        if pages > 1:
            kb.row(
                InlineKeyboardButton(text="◀️" if page > 0 else "·",
                                     callback_data=Nav(a="page", q=query_id, p=page - 1).pack() if page > 0
                                     else Nav(a="noop").pack()),
                InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data=Nav(a="noop").pack()),
                InlineKeyboardButton(text="▶️" if page < pages - 1 else "·",
                                     callback_data=Nav(a="page", q=query_id, p=page + 1).pack() if page < pages - 1
                                     else Nav(a="noop").pack()),
            )
        kb.row(InlineKeyboardButton(text="🏠 Меню", callback_data=Nav(a="menu").pack()))
        text = texts.search_results(query, len(hits), page, pages, self.catalog.current_year)
        return text, kb.as_markup()

    def card(self, o: CatalogOlympiad, telegram_id: int, nav: Nav) -> Screen:
        today = self.today()
        text = texts.olympiad_card(
            o, self.catalog.years, self.storage.upcoming_stages(o.id, today), today, detailed=bool(nav.d))
        return text, self.card_keyboard(o, bool(self.subscribed_ids(telegram_id, o.id)), nav)

    def card_keyboard(self, o: CatalogOlympiad, is_subscribed: bool, nav: Nav) -> InlineKeyboardMarkup:
        keep = dict(o=o.id, q=nav.q, p=nav.p, b=nav.b, d=nav.d)
        kb = InlineKeyboardBuilder()
        if is_subscribed:
            kb.row(InlineKeyboardButton(text="🔕 Отписаться", callback_data=Nav(a="unsub", **keep).pack()))
        else:
            kb.row(InlineKeyboardButton(text="🔔 Подписаться на этапы", callback_data=Nav(a="sub", **keep).pack()))
        row = []
        if not o.is_vsosh:
            toggle = dict(keep, d=0 if nav.d else 1)
            row.append(InlineKeyboardButton(text="📋 Кратко" if nav.d else "📋 Подробнее",
                                            callback_data=Nav(a="card", **toggle).pack()))
        if o.websites:
            row.append(InlineKeyboardButton(text="🌐 Сайт", url=o.websites[0]))
        if row:
            kb.row(*row)
        if nav.b == "s":
            back = InlineKeyboardButton(text="◀️ К результатам", callback_data=Nav(a="page", q=nav.q, p=nav.p).pack())
        elif nav.b == "u":
            back = InlineKeyboardButton(text="◀️ К подпискам", callback_data=Nav(a="subs").pack())
        else:
            back = None
        menu = InlineKeyboardButton(text="🏠 Меню", callback_data=Nav(a="menu").pack())
        kb.row(*([back, menu] if back else [menu]))
        return kb.as_markup()

    def search(self, query: str, telegram_id: int) -> Screen:
        """Экран после поиска: одна олимпиада — сразу карточка, иначе список."""
        query = " ".join(query.split())[:MAX_QUERY]
        hits = self.catalog.search(query, limit=2)
        if len(hits) == 1:
            return self.card(hits[0].olympiad, telegram_id, Nav(a="card", o=hits[0].olympiad.id))
        if not hits:
            kb = InlineKeyboardBuilder()
            kb.button(text="🏠 Меню", callback_data=Nav(a="menu"))
            return texts.NOT_FOUND.format(query=texts.escape(query)), kb.as_markup()
        return self.results(self.storage.save_query(query), 0)


async def show(callback: CallbackQuery, screen: Screen) -> None:
    """Меняет сообщение с кнопками на новый экран. Если изменить нельзя — присылает новое."""
    text, markup = screen
    message = callback.message
    if isinstance(message, Message):
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
            log.info("Не удалось изменить сообщение (%s), отправляю новое", exc)
    await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup)


def build_router(catalog: Catalog, storage: Storage, today: Callable[[], date]) -> Router:
    router = Router(name="olymp")
    views = Views(catalog, storage, today)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        storage.touch_user(message.chat.id)
        text, markup = views.menu()
        await message.answer(text, reply_markup=markup)

    @router.message(Command("help"))
    async def help_(message: Message) -> None:
        text, markup = views.help()
        await message.answer(text, reply_markup=markup)

    @router.message(Command("subs"))
    async def subs(message: Message) -> None:
        text, markup = views.subs(message.chat.id)
        await message.answer(text, reply_markup=markup)

    @router.message(F.text.startswith("/"))
    async def unknown_command(message: Message) -> None:
        await message.answer("Не знаю такой команды. Напиши название олимпиады или /help.")

    @router.message(F.text)
    async def search(message: Message) -> None:
        text, markup = views.search(message.text, message.chat.id)
        await message.answer(text, reply_markup=markup)

    @router.message()
    async def other(message: Message) -> None:
        await message.answer("Я понимаю только текст. Напиши название олимпиады или предмет.")

    @router.callback_query(Nav.filter())
    async def navigate(callback: CallbackQuery, callback_data: Nav) -> None:
        nav = callback_data
        user_id = callback.from_user.id
        if nav.a == "noop":
            await callback.answer()
            return
        if nav.a == "menu":
            await show(callback, views.menu())
        elif nav.a == "help":
            await show(callback, views.help())
        elif nav.a == "subs":
            await show(callback, views.subs(user_id))
        elif nav.a == "ex" and 0 <= nav.p < len(EXAMPLES):
            await show(callback, views.search(EXAMPLES[nav.p], user_id))
        elif nav.a == "page":
            screen = views.results(nav.q, nav.p)
            if screen is None:
                await callback.answer("Поиск устарел, отправь запрос заново.", show_alert=True)
                return
            await show(callback, screen)
        elif nav.a in ("card", "sub", "unsub"):
            o = catalog.get(nav.o)
            if o is None:
                await callback.answer("Олимпиада не найдена — перечень обновился. Поищи её заново.", show_alert=True)
                return
            if nav.a == "card":
                await show(callback, views.card(o, user_id, nav))
            else:
                await toggle_subscription(callback, o, nav)
                return
        await callback.answer()

    async def toggle_subscription(callback: CallbackQuery, o: CatalogOlympiad, nav: Nav) -> None:
        user_id = callback.from_user.id
        if nav.a == "sub":
            storage.subscribe(user_id, o.id)
            toast = texts.subscribed(o)
        else:
            for sid in views.subscribed_ids(user_id, o.id):
                storage.unsubscribe(user_id, sid)
            toast = texts.unsubscribed(o)
        markup = views.card_keyboard(o, bool(views.subscribed_ids(user_id, o.id)), nav)
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_reply_markup(reply_markup=markup)
            except TelegramBadRequest:
                pass  # кнопка уже в нужном состоянии (двойное нажатие)
        await callback.answer(toast)

    @router.callback_query()
    async def stale_button(callback: CallbackQuery) -> None:
        await callback.answer("Эта кнопка устарела. Нажми /start или отправь запрос заново.", show_alert=True)

    return router


def make_sender(bot: Bot):
    async def send(telegram_id: int, text: str) -> None:
        try:
            await bot.send_message(telegram_id, text)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            raise TemporaryError(str(exc)) from exc
        except TelegramForbiddenError as exc:
            raise UserBlocked(str(exc)) from exc
        except TelegramBadRequest as exc:
            if "chat not found" in str(exc).lower():
                raise UserBlocked(str(exc)) from exc
            raise
        except TelegramNetworkError as exc:
            raise TemporaryError(str(exc)) from exc

    return send


def build_dispatcher(catalog: Catalog, storage: Storage, today: Callable[[], date] | None = None) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(catalog, storage, today or (lambda: moscow_now().date())))
    return dp


def make_bot(token: str) -> Bot:
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))


async def run(token: str, catalog: Catalog, storage: Storage) -> None:
    bot = make_bot(token)
    dp = build_dispatcher(catalog, storage)
    await bot.set_my_commands([
        BotCommand(command="start", description="Меню"),
        BotCommand(command="subs", description="Мои подписки"),
        BotCommand(command="help", description="Как пользоваться"),
    ])
    me = await bot.get_me()
    log.info("Бот @%s запущен. Олимпиад в каталоге: %s, годы: %s",
             me.username, len(catalog.olympiads), ", ".join(catalog.years))
    reminders = asyncio.create_task(reminder_loop(storage, catalog, make_sender(bot)))
    try:
        await dp.start_polling(bot)
    finally:
        reminders.cancel()
        await bot.session.close()
