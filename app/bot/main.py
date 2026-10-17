"""Telegram-бот на aiogram 3.

Навигация — инлайн-кнопками в одном сообщении: меню → результаты поиска → карточка
олимпиады → «Назад». Новое сообщение бот отправляет только в ответ на сообщение
пользователя (поисковый запрос или команду).

Запуск: python -m app.cli bot run  (токен берётся из TELEGRAM_BOT_TOKEN в .env)
"""

from __future__ import annotations

import asyncio
import re
import logging
import math
import socket
from datetime import date
from pathlib import Path
from typing import Callable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.notifier import (
    TemporaryError, UserBlocked, benefits_updates_loop, benefits_watch_loop, forward_help_requests, help_requests_loop,
    moscow_now, reminder_loop, stages_sync_loop,
)
from app.bot.storage import Storage
from app.benefits import DIPLOMA_CHOICES, BenefitsCatalog, fitting, olympiad_profiles, profile_level
from app.catalog import Catalog, CatalogOlympiad

log = logging.getLogger(__name__)

PAGE_SIZE = 5
MAX_RESULTS = 200
MAX_QUERY = 100
LEVELS = (1, 2, 3)


class Nav(CallbackData, prefix="n"):
    """Данные кнопок. Вся навигация — через этот класс (лимит Telegram 64 байта).

    a — действие: menu, help, subs, lvl (список уровня), vsosh, page (результаты поиска),
        card, sub, unsub, ben (льготы олимпиады), benu (льготы олимпиады в вузе), unis (список вузов),
        uni (вуз), pdfp (выбор профиля для PDF), pdf / pdff (прислать PDF: по профилю / по итогу подбора),
        usub / uuns (подписка на обновления вуза, q — номер вуза), hlp / hlps (сопровождение: экран / заявка), fit / fitp / fitw / fity / fitr (подбор по диплому: профиль → победитель/призёр → класс → год диплома → итог), noop
    o — id олимпиады; q — id сохранённого запроса или номер уровня; p — страница списка
    b — куда ведёт «Назад» из карточки: "" — меню, "s" — результаты поиска, "l" — список уровня,
        "u" — подписки
    d — 1, если карточка открыта в подробном виде; в benu — номер вуза; в подборе — выбор:
        номер года диплома * 100000 + номер профиля * 100 + (1 победитель | 2 призёр) * 10 + (класс − 8);
        год 0 — текущий сезон (так кнопки старых версий без года продолжают работать)
    """

    a: str
    o: str = ""
    q: int = 0
    p: int = 0
    b: str = ""
    d: int = 0


Screen = tuple[str, InlineKeyboardMarkup]
YEAR_STEP = 100000


def decode_fit(d: int) -> tuple[int, int, int, int]:
    """Код выбора в подборе → (номер года диплома, номер профиля, диплом, класс − 8)."""
    rest = d % YEAR_STEP
    return d // YEAR_STEP, rest // 100, rest // 10 % 10, rest % 10


class Views:
    """Экраны бота: текст + кнопки. Не отправляют сообщения сами."""

    def __init__(self, catalog: Catalog, storage: Storage, today: Callable[[], date],
                 benefits: BenefitsCatalog | None = None):
        self.catalog = catalog
        # диплом действует 4 года после олимпиады: на приём следующего лета годятся 5 последних перечней
        self.diploma_years = catalog.years[:5]
        self.storage = storage
        self.today = today
        self.benefits = benefits or BenefitsCatalog()

    @property
    def admission_year(self) -> int:
        """Ближайший приём: учебный год 2026/27 — приём летом 2027."""
        return int(self.catalog.current_year[:4]) + 1

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
        for level in LEVELS:
            count = len(self.level_items(level))
            kb.button(text=f"{texts.LEVEL_ICONS[level]} уровень · {count}", callback_data=Nav(a="lvl", q=level))
        kb.button(text="🏆 ВсОШ", callback_data=Nav(a="vsosh"))
        kb.button(text="🎓 Вузы", callback_data=Nav(a="unis"))
        kb.button(text="🔔 Подписки", callback_data=Nav(a="subs"))
        kb.button(text="🤝 Сопровождение", callback_data=Nav(a="hlp"))
        kb.button(text="ℹ️ Помощь", callback_data=Nav(a="help"))
        kb.adjust(2)  # шашечкой: по две кнопки в ряд
        return texts.START, kb.as_markup()

    def help_offer(self, telegram_id: int) -> Screen:
        kb = InlineKeyboardBuilder()
        if self.storage.recent_help_request(telegram_id=telegram_id):
            text = texts.HELP_OFFER + "\n\n" + texts.HELP_ALREADY
        else:
            text = texts.HELP_OFFER
            kb.row(InlineKeyboardButton(text="✅ Оставить заявку", callback_data=Nav(a="hlps").pack()))
        kb.row(InlineKeyboardButton(text="🏠 Меню", callback_data=Nav(a="menu").pack()))
        return text, kb.as_markup()

    # -- списки по уровням ------------------------------------------------------

    def level_items(self, level: int) -> list[tuple[CatalogOlympiad, list[str]]]:
        """Олимпиады, у которых в текущем перечне есть профили этого уровня, — по алфавиту."""
        year = self.catalog.current_year
        items = []
        for o in self.catalog.olympiads.values():
            if o.is_vsosh:
                continue
            profiles = sorted(name for name, p in o.profiles_in(year).items() if p.level == level)
            if profiles:
                items.append((o, profiles))
        return sorted(items, key=lambda item: texts.sort_key(texts.button_name(item[0])))

    def level_list(self, level: int, page: int) -> Screen:
        items = self.level_items(level)
        page, pages, chunk = paginate(items, page)
        first = page * PAGE_SIZE + 1
        kb = InlineKeyboardBuilder()
        for number, (o, _) in enumerate(chunk, start=first):
            kb.row(InlineKeyboardButton(
                text=texts.left_align(f"{number}. {texts.button_name(o, 45)}"),
                callback_data=Nav(a="card", o=o.id, q=level, p=page, b="l").pack()))
        nav_row = pager_row(page, pages, lambda p: Nav(a="lvl", q=level, p=p))
        if nav_row:
            kb.row(*nav_row)
        kb.row(InlineKeyboardButton(text="🏠 Меню", callback_data=Nav(a="menu").pack()))
        text = texts.level_list(level, chunk, len(items), page, pages, first, self.catalog.current_year)
        return text, kb.as_markup()

    def help(self) -> Screen:
        kb = InlineKeyboardBuilder()
        kb.button(text="🏠 Меню", callback_data=Nav(a="menu"))
        return texts.HELP, kb.as_markup()

    def subs(self, telegram_id: int) -> Screen:
        olympiads = self.user_subscriptions(telegram_id)
        kb = InlineKeyboardBuilder()
        for o in olympiads:
            kb.button(text=texts.left_align(texts.button_name(o)), callback_data=Nav(a="card", o=o.id, b="u"))
        kb.button(text="🏠 Меню", callback_data=Nav(a="menu"))
        kb.adjust(1)
        return texts.subscriptions_list(olympiads), kb.as_markup()

    def results(self, query_id: int, page: int) -> Screen | None:
        query = self.storage.get_query(query_id)
        if query is None:
            return None
        hits = self.catalog.search(query, limit=MAX_RESULTS)
        page, pages, chunk = paginate(hits, page)
        kb = InlineKeyboardBuilder()
        for hit in chunk:
            label = texts.left_align(texts.hit_button_label(hit, self.catalog.current_year))
            kb.row(InlineKeyboardButton(
                text=label, callback_data=Nav(a="card", o=hit.olympiad.id, q=query_id, p=page, b="s").pack()))
        nav_row = pager_row(page, pages, lambda p: Nav(a="page", q=query_id, p=p))
        if nav_row:
            kb.row(*nav_row)
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
        count = len(self.benefits.for_olympiad(o.id))
        if count:
            kb.row(InlineKeyboardButton(text=f"🎓 Льготы в вузах · {count}",
                                        callback_data=Nav(a="ben", **keep).pack()))
        if nav.b == "s":
            back = InlineKeyboardButton(text="◀️ К результатам", callback_data=Nav(a="page", q=nav.q, p=nav.p).pack())
        elif nav.b == "l":
            back = InlineKeyboardButton(text=f"◀️ К {nav.q} уровню", callback_data=Nav(a="lvl", q=nav.q, p=nav.p).pack())
        elif nav.b == "u":
            back = InlineKeyboardButton(text="◀️ К подпискам", callback_data=Nav(a="subs").pack())
        else:
            back = None
        menu = InlineKeyboardButton(text="🏠 Меню", callback_data=Nav(a="menu").pack())
        kb.row(*([back, menu] if back else [menu]))
        return kb.as_markup()

    def olympiad_benefits(self, o: CatalogOlympiad, nav: Nav) -> Screen:
        entries = self.benefits.for_olympiad(o.id)
        text = texts.olympiad_benefits(o, entries, self.admission_year)
        kb = InlineKeyboardBuilder()
        unis = self.benefits.latest()
        buttons = [InlineKeyboardButton(text=f"📄 {u.university}", callback_data=Nav(
            a="benu", o=o.id, q=nav.q, p=nav.p, b=nav.b, d=unis.index(u)).pack()) for u, _ in entries]
        keep = dict(o=o.id, q=nav.q, p=nav.p, b=nav.b)
        if entries:
            kb.row(InlineKeyboardButton(text="🎯 Подобрать по моему диплому", callback_data=Nav(a="fit", **keep).pack()))
            kb.row(InlineKeyboardButton(text="📄 Скачать PDF", callback_data=Nav(a="pdfp", **keep).pack()))
        for i in range(0, len(buttons), 2):
            kb.row(*buttons[i:i + 2])
        kb.row(InlineKeyboardButton(text="◀️ К олимпиаде", callback_data=Nav(
            a="card", o=o.id, q=nav.q, p=nav.p, b=nav.b, d=nav.d).pack()),
            InlineKeyboardButton(text="🏠 Меню", callback_data=Nav(a="menu").pack()))
        return text, kb.as_markup()

    def olympiad_university(self, o: CatalogOlympiad, nav: Nav) -> Screen | None:
        """Льготы олимпиады в одном вузе (индекс вуза — в nav.d)."""
        unis = self.benefits.latest()
        if not 0 <= nav.d < len(unis):
            return None
        u = unis[nav.d]
        rules = [r for r in u.rules if r.olympiad.id == o.id]
        if not rules:
            return None
        text = texts.olympiad_university_benefits(o, u, rules, self.admission_year)
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="◀️ Ко всем вузам", callback_data=Nav(
            a="ben", o=o.id, q=nav.q, p=nav.p, b=nav.b).pack()),
            InlineKeyboardButton(text="🏠 Меню", callback_data=Nav(a="menu").pack()))
        return text, kb.as_markup()

    def diploma_fit(self, o: CatalogOlympiad, nav: Nav) -> Screen | None:
        """Подбор по диплому. Шаги: fit — профиль, fitp — победитель/призёр, fitw — класс,
        fity — год диплома (если уровень профиля менялся), fitr — итог."""
        profiles = olympiad_profiles(self.benefits, o.id)
        if not profiles:
            return None
        keep = dict(o=o.id, q=nav.q, p=nav.p, b=nav.b)
        step = nav.a
        if step == "fit" and len(profiles) == 1:
            step, nav = "fitp", Nav(a="fitp", d=0, **keep)
        yi, pi, dip, grade_code = decode_fit(nav.d)
        if pi >= len(profiles) or yi >= len(self.diploma_years) or (
                step in ("fitw", "fity", "fitr") and dip not in DIPLOMA_CHOICES):
            return None
        profile = profiles[pi]
        years = self.diploma_years
        # год диплома спрашиваем, только если уровень профиля в эти годы менялся
        ask_year = len({profile_level(o, profile, y)[0] for y in years}) > 1 and not o.is_vsosh
        base = pi * 100 + dip * 10 + grade_code
        diploma = DIPLOMA_CHOICES.get(dip)
        back_to_benefits = InlineKeyboardButton(text="🎓 К льготам", callback_data=Nav(a="ben", **keep).pack())
        kb = InlineKeyboardBuilder()
        if step == "fit":
            for i, name in enumerate(profiles):
                kb.button(text=name[:40], callback_data=Nav(a="fitp", d=i * 100, **keep))
            kb.adjust(2)
            kb.row(back_to_benefits)
            return texts.fit_question(o, "profile"), kb.as_markup()
        prev = {"fitp": ("fit", 0), "fitw": ("fitp", pi * 100), "fity": ("fitw", pi * 100 + dip * 10),
                "fitr": ("fity", base) if ask_year else ("fitw", pi * 100 + dip * 10)}[step]
        back_step = InlineKeyboardButton(text="◀️ Назад", callback_data=Nav(a=prev[0], d=prev[1], **keep).pack())
        if step == "fitp":
            kb.row(InlineKeyboardButton(text="🥇 Победитель", callback_data=Nav(a="fitw", d=pi * 100 + 10, **keep).pack()),
                   InlineKeyboardButton(text="🥈 Призёр", callback_data=Nav(a="fitw", d=pi * 100 + 20, **keep).pack()))
            kb.row(back_step, back_to_benefits)
            return texts.fit_question(o, "diploma", profile), kb.as_markup()
        if step == "fitw":
            grades = (9, 10, 11) if o.is_vsosh else (10, 11)
            kb.row(*[InlineKeyboardButton(text=f"{g} класс", callback_data=Nav(
                a="fity" if ask_year else "fitr", d=pi * 100 + dip * 10 + g - 8, **keep).pack()) for g in grades])
            kb.row(back_step, back_to_benefits)
            return texts.fit_question(o, "grade", profile, diploma), kb.as_markup()
        if step == "fity":
            for i, y in enumerate(years):
                if y not in o.profiles.get(profile, {}):
                    continue  # профиля не было в перечне — диплом того года льгот не даёт
                level = o.profiles[profile][y].level
                label = f"{y}" + (" · этот сезон" if i == 0 else "") + (f" · {level} уровень" if level else "")
                kb.button(text=label, callback_data=Nav(a="fitr", d=i * YEAR_STEP + base, **keep))
            kb.adjust(1)
            kb.row(back_step, back_to_benefits)
            return texts.fit_question(o, "year", profile, diploma), kb.as_markup()
        grade = grade_code + 8 if grade_code else None
        level, level_year = profile_level(o, profile, years[yi])
        entries = fitting(self.benefits, o.id, profile, diploma, grade, level)
        text = texts.fit_result(o, profile, diploma, grade, level, level_year, entries, self.admission_year)
        unis = self.benefits.latest()
        buttons = [InlineKeyboardButton(text=f"📄 {u.university}", callback_data=Nav(
            a="benu", d=unis.index(u), **keep).pack()) for u, _ in entries]
        for i in range(0, len(buttons), 3):
            kb.row(*buttons[i:i + 3])
        if entries:
            kb.row(InlineKeyboardButton(text="📄 Скачать PDF", callback_data=Nav(a="pdff", d=nav.d, **keep).pack()))
        kb.row(InlineKeyboardButton(text="🔄 Другой диплом", callback_data=Nav(a="fit", **keep).pack()),
               back_to_benefits)
        return text, kb.as_markup()

    def pdf_profiles(self, o: CatalogOlympiad, nav: Nav) -> Screen | None:
        """Перед PDF — выбор профиля: по всей олимпиаде файл вышел бы на сотню страниц."""
        profiles = olympiad_profiles(self.benefits, o.id)
        if not profiles:
            return None
        keep = dict(o=o.id, q=nav.q, p=nav.p, b=nav.b)
        kb = InlineKeyboardBuilder()
        for i, name in enumerate(profiles):
            kb.button(text=name[:40], callback_data=Nav(a="pdf", d=i * 100, **keep))
        kb.adjust(2)
        kb.row(InlineKeyboardButton(text="🎓 К льготам", callback_data=Nav(a="ben", **keep).pack()))
        name = texts.escape("ВсОШ" if o.is_vsosh else texts.short_name(o.name, 60))
        return (f"📄 <b>PDF со льготами: «{name}»</b>\n\nПо какому профилю? В файле — все вузы, все программы "
                f"и условия по ЕГЭ для этого профиля."), kb.as_markup()

    def pdf_document(self, o: CatalogOlympiad, nav: Nav) -> tuple[bytes, str, str] | None:
        """(файл, имя, подпись). pdf — по профилю целиком; pdff — по итогу подбора (профиль, диплом, класс)."""
        from app.export_pdf import benefits_pdf, pdf_filename

        profiles = olympiad_profiles(self.benefits, o.id)
        yi, pi, dip, grade_code = decode_fit(nav.d)
        if pi >= len(profiles) or yi >= len(self.diploma_years):
            return None
        profile = profiles[pi]
        level, level_year = profile_level(o, profile, self.diploma_years[yi])
        notes = []
        if nav.a == "pdff":
            diploma = DIPLOMA_CHOICES.get(dip)
            if diploma is None:
                return None
            grade = grade_code + 8 if grade_code else None
            entries = fitting(self.benefits, o.id, profile, diploma, grade, level)
            parts = [f"профиль «{profile}»", texts.DIPLOMA_TITLES[diploma]] + ([f"{grade} класс"] if grade else [])
            if level and not o.is_vsosh:
                parts.append(f"уровень {level} в перечне {level_year}")
            subtitle = " · ".join(parts).capitalize()
            if diploma == "prize" and texts.prize_bvi_affected([r for _, rs in entries for r in rs]):
                notes.append(texts.PRIZE_BVI_WARNING)
        else:
            entries = [(u, [r for r in rules if not r.profiles or profile in r.profiles])
                       for u, rules in self.benefits.for_olympiad(o.id)]
            entries = [(u, rules) for u, rules in entries if rules]
            subtitle = f"Профиль «{profile}» · победители и призёры · все вузы"
            if texts.prize_bvi_affected([r for _, rs in entries for r in rs]):
                notes.append(texts.PRIZE_BVI_WARNING)
        notes += texts.not_in_list_banner(o, [r for _, rs in entries for r in rs], self.admission_year)
        data = benefits_pdf(o, entries, self.admission_year, subtitle=subtitle, notes=notes, today=self.today())
        name = texts.escape("ВсОШ" if o.is_vsosh else texts.short_name(o.name, 60))
        caption = f"📄 Льготы: «{name}» — {texts.escape(subtitle[0].lower() + subtitle[1:])}"
        return data, pdf_filename(o, _slug(profile)), caption

    def universities(self) -> Screen:
        unis = self.benefits.latest()
        kb = InlineKeyboardBuilder()
        for index, u in enumerate(unis):
            kb.button(text=texts.left_align(u.university), callback_data=Nav(a="uni", q=index))
        kb.button(text="🏠 Меню", callback_data=Nav(a="menu"))
        kb.adjust(1)
        return texts.universities_list(unis, self.admission_year), kb.as_markup()

    def university(self, index: int, telegram_id: int = 0) -> Screen | None:
        unis = self.benefits.latest()
        if not 0 <= index < len(unis):
            return None
        u = unis[index]
        kb = InlineKeyboardBuilder()
        if self.storage.is_subscribed_university(telegram_id, u.university):
            kb.row(InlineKeyboardButton(text="🔕 Не сообщать об обновлениях",
                                        callback_data=Nav(a="uuns", q=index).pack()))
        else:
            label = (f"🔔 Сообщить, когда выйдут льготы на {self.admission_year}"
                     if u.admission_year < self.admission_year or u.is_draft else "🔔 Сообщать об обновлениях")
            kb.row(InlineKeyboardButton(text=label, callback_data=Nav(a="usub", q=index).pack()))
        kb.row(InlineKeyboardButton(text="◀️ К вузам", callback_data=Nav(a="unis").pack()),
               InlineKeyboardButton(text="🏠 Меню", callback_data=Nav(a="menu").pack()))
        return texts.university_benefits(u, self.admission_year), kb.as_markup()

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


def _slug(text: str) -> str:
    from app.export_pdf import _translit

    return re.sub(r"[^a-z0-9]+", "-", _translit(text.lower())).strip("-")[:30]


def paginate(items: list, page: int) -> tuple[int, int, list]:
    """(страница, всего страниц, элементы страницы). Номер страницы приводится к допустимому."""
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    return page, pages, items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]


def pager_row(page: int, pages: int, make: Callable[[int], Nav]) -> list[InlineKeyboardButton]:
    """◀️ 2 / 7 ▶️ — листание по кругу: с последней страницы ▶️ ведёт на первую."""
    if pages <= 1:
        return []
    return [
        InlineKeyboardButton(text="◀️", callback_data=make((page - 1) % pages).pack()),
        InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data=Nav(a="noop").pack()),
        InlineKeyboardButton(text="▶️", callback_data=make((page + 1) % pages).pack()),
    ]


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


def build_router(catalog: Catalog, storage: Storage, today: Callable[[], date],
                 benefits: BenefitsCatalog | None = None) -> Router:
    router = Router(name="olymp")
    views = Views(catalog, storage, today, benefits)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        storage.touch_user(message.chat.id)
        payload = (message.text or "").partition(" ")[2].strip()
        if payload.startswith("login_"):
            # вход на сайт: сайт показал ссылку t.me/<бот>?start=login_<токен>
            if storage.confirm_login(payload.removeprefix("login_"), message.chat.id):
                await message.answer("✅ Готово — вернись на сайт, вход выполнится сам.\n\n"
                                     "<i>Если вкладку закрыли, нажми «Войти» на сайте ещё раз.</i>")
            else:
                await message.answer("Ссылка для входа устарела. Нажми «Войти» на сайте ещё раз.")
            return
        if payload == "error":
            await message.answer(texts.REPORT_ERROR_HINT)
            return
        if payload == "help":
            text, markup = views.help_offer(message.chat.id)
            await message.answer(text, reply_markup=markup)
            return
        text, markup = views.menu()
        await message.answer(text, reply_markup=markup)

    @router.message(F.text.regexp(r"(?i)^ошибка\b"))
    async def report_error(message: Message) -> None:
        from app.config import settings

        if settings.bot_admin_id:
            user = message.from_user
            who = f"@{user.username}" if user and user.username else f"id {message.chat.id}"
            await message.bot.send_message(settings.bot_admin_id, f"📮 Сообщение об ошибке от {texts.escape(who)}:\n\n"
                                                                 f"{texts.escape(message.text or '')}")
        await message.answer("Спасибо! Передали — проверим по документу вуза и исправим.")

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
        elif nav.a == "hlp":
            await show(callback, views.help_offer(user_id))
        elif nav.a == "hlps":
            from app.config import settings

            if not storage.recent_help_request(telegram_id=user_id):
                user = callback.from_user
                storage.add_help_request(source="bot", telegram_id=user_id, username=user.username or "",
                                         name=" ".join(x for x in (user.first_name, user.last_name) if x))
                if settings.bot_admin_id:
                    await forward_help_requests(storage, catalog, make_sender(callback.bot), settings.bot_admin_id)
            kb = InlineKeyboardBuilder()
            kb.row(InlineKeyboardButton(text="🏠 Меню", callback_data=Nav(a="menu").pack()))
            await show(callback, (texts.HELP_SENT, kb.as_markup()))
        elif nav.a == "subs":
            await show(callback, views.subs(user_id))
        elif nav.a == "lvl" and nav.q in LEVELS:
            await show(callback, views.level_list(nav.q, nav.p))
        elif nav.a == "unis":
            await show(callback, views.universities())
        elif nav.a in ("usub", "uuns"):
            unis = views.benefits.latest()
            if not 0 <= nav.q < len(unis):
                await stale_button(callback)
                return
            name = unis[nav.q].university
            if nav.a == "usub":
                storage.subscribe_university(user_id, name)
                toast = f"Сообщу, когда {name} опубликует новые документы о льготах"
            else:
                storage.unsubscribe_university(user_id, name)
                toast = f"Больше не сообщаю об обновлениях {name}"
            await show(callback, views.university(nav.q, user_id))
            await callback.answer(toast)
            return
        elif nav.a == "uni":
            screen = views.university(nav.q, user_id)
            if screen is None:
                await stale_button(callback)
                return
            await show(callback, screen)
        elif nav.a == "vsosh":
            vsosh = next((o for o in catalog.olympiads.values() if o.is_vsosh), None)
            if vsosh is None:
                await callback.answer("ВсОШ нет в каталоге.", show_alert=True)
                return
            await show(callback, views.card(vsosh, user_id, Nav(a="card", o=vsosh.id)))
        elif nav.a == "page":
            screen = views.results(nav.q, nav.p)
            if screen is None:
                await callback.answer("Поиск устарел, отправь запрос заново.", show_alert=True)
                return
            await show(callback, screen)
        elif nav.a in ("card", "sub", "unsub", "ben", "benu", "fit", "fitp", "fitw", "fity", "fitr", "pdfp", "pdf", "pdff"):
            o = catalog.get(nav.o)
            if o is None:
                await callback.answer("Олимпиада не найдена — перечень обновился. Поищи её заново.", show_alert=True)
                return
            if nav.a == "card":
                await show(callback, views.card(o, user_id, nav))
            elif nav.a == "ben":
                await show(callback, views.olympiad_benefits(o, nav))
            elif nav.a in ("pdf", "pdff"):
                await send_pdf(callback, o, nav)
                return
            elif nav.a == "pdfp":
                profiles = olympiad_profiles(views.benefits, o.id)
                if len(profiles) == 1:  # профиль один — сразу файл
                    await send_pdf(callback, o, Nav(a="pdf", o=o.id, q=nav.q, p=nav.p, b=nav.b, d=0))
                    return
                screen = views.pdf_profiles(o, nav)
                if screen is None:
                    await stale_button(callback)
                    return
                await show(callback, screen)
            elif nav.a in ("benu", "fit", "fitp", "fitw", "fity", "fitr"):
                screen = views.olympiad_university(o, nav) if nav.a == "benu" else views.diploma_fit(o, nav)
                if screen is None:
                    await stale_button(callback)
                    return
                await show(callback, screen)
            else:
                await toggle_subscription(callback, o, nav)
                return
        else:
            await stale_button(callback)
            return
        await callback.answer()

    async def send_pdf(callback: CallbackQuery, o: CatalogOlympiad, nav: Nav) -> None:
        await callback.answer("Готовлю PDF…")
        made = await asyncio.to_thread(views.pdf_document, o, nav)
        if not isinstance(callback.message, Message):
            return
        if made is None:
            await callback.message.answer("Эта кнопка устарела — открой льготы олимпиады заново.")
            return
        data, filename, caption = made
        await callback.message.answer_document(BufferedInputFile(data, filename=filename), caption=caption)

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


def build_dispatcher(catalog: Catalog, storage: Storage, today: Callable[[], date] | None = None,
                     benefits: BenefitsCatalog | None = None) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(catalog, storage, today or (lambda: moscow_now().date()), benefits))
    return dp


def make_bot(token: str, proxy: str = "", force_ipv4: bool = True) -> Bot:
    """Бот с настройками соединения.

    force_ipv4: у многих провайдеров IPv6-маршрут до Telegram «висит» — опрос при этом
    работает, а ответы падают с «Request timeout». Принудительный IPv4 это лечит.
    proxy: http(s):// или socks5:// — если провайдер замедляет Telegram (нужен пакет aiohttp-socks).
    """
    if proxy:
        session = AiohttpSession(proxy=proxy)
    else:
        session = AiohttpSession()
        if force_ipv4:
            session._connector_init["family"] = socket.AF_INET  # noqa: SLF001 — у aiogram нет публичной настройки
    return Bot(token, session=session,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))


async def save_bot_avatar(bot: Bot, bot_id: int, folder: Path) -> Path | None:
    """Скачивает аватарку бота в data/avatar.jpg — она идёт водяным знаком в PDF.
    Свою картинку можно положить в data/avatar.png: она важнее и не перезаписывается."""
    try:
        photos = await bot.get_user_profile_photos(bot_id, limit=1)
        if not photos.photos:
            log.info("У бота в Telegram нет аватарки — в PDF водяной знак из app/assets/logo.png")
            return None
        file = await bot.get_file(photos.photos[0][-1].file_id)
        target = folder / "avatar.jpg"
        folder.mkdir(parents=True, exist_ok=True)
        await bot.download_file(file.file_path, destination=target)
        return target
    except Exception:  # noqa: BLE001 — без аватарки бот работает, PDF просто без водяного знака
        log.warning("Не удалось скачать аватарку бота", exc_info=True)
        return None


async def run(token: str, catalog: Catalog, storage: Storage, proxy: str = "", force_ipv4: bool = True,
              stages_autosync: bool = True, benefits: BenefitsCatalog | None = None) -> None:
    bot = make_bot(token, proxy=proxy, force_ipv4=force_ipv4)
    log.info("Соединение с Telegram: %s", f"через прокси {proxy.split('@')[-1]}" if proxy
             else ("только IPv4" if force_ipv4 else "IPv4 и IPv6"))
    dp = build_dispatcher(catalog, storage, benefits=benefits)
    await bot.set_my_commands([
        BotCommand(command="start", description="Меню"),
        BotCommand(command="subs", description="Мои подписки"),
        BotCommand(command="help", description="Как пользоваться"),
    ])
    me = await bot.get_me()
    # Если у бота остался вебхук (например, от другого сервиса), getUpdates не получит сообщений
    webhook = await bot.get_webhook_info()
    if webhook.url:
        log.warning("У бота был установлен вебхук %s — удаляю, чтобы работал опрос", webhook.url)
        await bot.delete_webhook()
    if storage.path != ":memory:":
        from app.config import settings as app_settings

        await save_bot_avatar(bot, me.id, Path(app_settings.data_dir))
    log.info("Бот @%s запущен. Олимпиад в каталоге: %s, годы: %s",
             me.username, len(catalog.olympiads), ", ".join(catalog.years))
    print(f"\nНапишите боту: https://t.me/{me.username}  (каждое сообщение появится здесь строкой «Update id=…»)\n")
    tasks = [asyncio.create_task(reminder_loop(storage, catalog, make_sender(bot)))]
    if stages_autosync and storage.path != ":memory:":
        from app.fetch import fetch_html

        tasks.append(asyncio.create_task(stages_sync_loop(catalog, storage.path, fetch_html)))
    if storage.path != ":memory:":
        from app.config import PROJECT_ROOT, settings

        admission_year = int(catalog.current_year[:4]) + 1
        tasks.append(asyncio.create_task(benefits_updates_loop(
            storage, catalog, benefits or BenefitsCatalog(), make_sender(bot), admin_id=settings.bot_admin_id)))
        tasks.append(asyncio.create_task(benefits_watch_loop(
            storage.path, PROJECT_ROOT / "benefits" / "watch" / "sources.json", admission_year,
            make_sender(bot), admin_id=settings.bot_admin_id)))
        tasks.append(asyncio.create_task(help_requests_loop(storage.path, catalog, make_sender(bot),
                                                            settings.bot_admin_id)))
    try:
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()
        await bot.session.close()
