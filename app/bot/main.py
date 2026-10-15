"""Telegram-бот на aiogram 3.

Запуск: python -m app.cli bot run  (токен берётся из TELEGRAM_BOT_TOKEN в .env)
"""

from __future__ import annotations

import asyncio
import logging
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
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.notifier import TemporaryError, UserBlocked, moscow_now, reminder_loop
from app.bot.storage import Storage
from app.catalog import Catalog, CatalogOlympiad

log = logging.getLogger(__name__)

SEARCH_LIMIT = 10
MAX_QUERY = 100


class OlympiadCb(CallbackData, prefix="o"):
    action: str   # open | sub | unsub
    id: str


def card_keyboard(o: CatalogOlympiad, is_subscribed: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if is_subscribed:
        kb.button(text="🔕 Отписаться", callback_data=OlympiadCb(action="unsub", id=o.id))
    else:
        kb.button(text="🔔 Подписаться на этапы", callback_data=OlympiadCb(action="sub", id=o.id))
    return kb.as_markup()


def list_keyboard(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Кнопки-олимпиады: (подпись, id)."""
    kb = InlineKeyboardBuilder()
    for label, olympiad_id in items:
        kb.button(text=label, callback_data=OlympiadCb(action="open", id=olympiad_id))
    kb.adjust(1)
    return kb.as_markup()


def build_router(catalog: Catalog, storage: Storage, today: Callable[[], date]) -> Router:
    router = Router(name="olymp")

    def subscribed_ids(telegram_id: int, olympiad_id: str) -> list[str]:
        """Id, под которыми пользователь подписан на олимпиаду (включая старые id до переименования)."""
        return [sid for sid in storage.subscriptions(telegram_id) if catalog.canonical_id(sid) == olympiad_id]

    def card(o: CatalogOlympiad, telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
        upcoming = storage.upcoming_stages(o.id, today())
        text = texts.olympiad_card(o, catalog.current_year, upcoming, today())
        return text, card_keyboard(o, bool(subscribed_ids(telegram_id, o.id)))

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        storage.touch_user(message.chat.id)
        await message.answer(texts.START)

    @router.message(Command("help"))
    async def help_(message: Message) -> None:
        await message.answer(texts.HELP)

    @router.message(Command("subs"))
    async def subs(message: Message) -> None:
        olympiads: list[CatalogOlympiad] = []
        for oid in storage.subscriptions(message.chat.id):
            o = catalog.get(oid)
            if o and o not in olympiads:
                olympiads.append(o)
        markup = list_keyboard([(texts.short_name(o.name, 60), o.id) for o in olympiads]) if olympiads else None
        await message.answer(texts.subscriptions_list(olympiads), reply_markup=markup)

    @router.message(F.text & ~F.text.startswith("/"))
    async def search(message: Message) -> None:
        query = message.text.strip()[:MAX_QUERY]
        hits = catalog.search(query, limit=SEARCH_LIMIT + 1)
        more = len(hits) > SEARCH_LIMIT
        hits = hits[:SEARCH_LIMIT]
        if len(hits) == 1:
            text, markup = card(hits[0].olympiad, message.chat.id)
            await message.answer(text, reply_markup=markup)
            return
        markup = list_keyboard([(texts.hit_button_label(h, catalog.current_year), h.olympiad.id) for h in hits])
        await message.answer(texts.search_results(query, hits, more), reply_markup=markup if hits else None)

    @router.message(F.text.startswith("/"))
    async def unknown_command(message: Message) -> None:
        await message.answer("Не знаю такой команды. Напиши название олимпиады или /help.")

    @router.callback_query(OlympiadCb.filter())
    async def on_olympiad(callback: CallbackQuery, callback_data: OlympiadCb) -> None:
        o = catalog.get(callback_data.id)
        if o is None:
            await callback.answer("Олимпиада не найдена, перечень обновился. Поищи её заново.", show_alert=True)
            return
        user_id = callback.from_user.id
        if callback_data.action == "open":
            text, markup = card(o, user_id)
            await callback.message.answer(text, reply_markup=markup)
            await callback.answer()
            return
        if callback_data.action == "sub":
            storage.subscribe(user_id, o.id)
            toast = texts.subscribed(o)
        else:
            for sid in subscribed_ids(user_id, o.id):
                storage.unsubscribe(user_id, sid)
            toast = texts.unsubscribed(o)
        try:
            await callback.message.edit_reply_markup(reply_markup=card_keyboard(o, bool(subscribed_ids(user_id, o.id))))
        except TelegramBadRequest:
            pass  # кнопка уже в нужном состоянии (двойной клик)
        await callback.answer(toast)

    @router.message()
    async def other(message: Message) -> None:
        await message.answer("Я понимаю только текст. Напиши название олимпиады или предмет.")

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
        BotCommand(command="start", description="Начало"),
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
