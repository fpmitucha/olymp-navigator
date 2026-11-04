"""Сайт Бвишечки: олимпиады, вузы, льготы, подбор по диплому и личный кабинет — те же данные, что у бота.

Запуск: py -m app.cli web run (http://127.0.0.1:8000). Страницы рендерятся на сервере (Jinja2),
у каждой льготы — ссылка на документ вуза и дата проверки. JSON — /api/…

Личный кабинет: вход через бота (ссылка t.me/<бот>?start=login_<токен>, бот подтверждает вход в общей базе),
дипломы, вузы и олимпиады, за которыми следишь (те же подписки, что в боте), ближайшие этапы.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterator

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.benefits import (
    BENEFIT_TYPES, DIPLOMA_CHOICES, DIPLOMAS, BenefitsCatalog, Rule, UniversityBenefits, fitting, olympiad_profiles,
    profile_level,
)
from app.bot.storage import Storage
from app.catalog import Catalog, CatalogOlympiad

HERE = Path(__file__).parent
BOT_USERNAME = "bvishechka_bot"
BOT_URL = f"https://t.me/{BOT_USERNAME}"
SESSION_COOKIE = "bv_sid"
SESSION_DAYS = 30   # вход живёт 30 дней с последнего захода
ROMAN = {1: "I", 2: "II", 3: "III"}
_EMOJI = re.compile(r"<[^>]+>|[\U0001F000-\U0001FFFF☀-➿️]")


def slug(text: str) -> str:
    from app.export_pdf import _translit

    return re.sub(r"[^a-z0-9]+", "-", _translit(text.lower())).strip("-") or "vuz"


def plain(html: str) -> str:
    """Текст бота → строка для сайта: без тегов и эмодзи."""
    return _EMOJI.sub("", html).strip()


@dataclass
class BenefitGroup:
    """Сводка одной льготы вуза для олимпиады: «БВИ — победителям и призёрам: математика (12 программ)»."""
    benefit: str
    diploma: str
    profiles: list[tuple[str, int]]   # (профиль, число программ; 0 — «по профилю олимпиады»)
    scores: list[int]


def summarize(rules: list[Rule]) -> list[BenefitGroup]:
    groups: dict[tuple[str, str], dict[str, set[str]]] = {}
    scores: dict[tuple[str, str], set[int]] = {}
    for r in rules:
        key = (r.benefit, r.diploma)
        by_profile = groups.setdefault(key, {})
        for p in r.profiles or ["любой профиль"]:
            by_profile.setdefault(p, set()).update(r.programs or ())
        if r.confirm_min_score:
            scores.setdefault(key, set()).add(r.confirm_min_score)
    order = sorted(groups, key=lambda k: (list(BENEFIT_TYPES).index(k[0]), k[1] != "any"))
    return [BenefitGroup(b, d, sorted((p, len(progs)) for p, progs in groups[(b, d)].items()),
                         sorted(scores.get((b, d), ()))) for b, d in order]


def best_benefit(rules: list[Rule]) -> str | None:
    """Самая сильная льгота из правил: БВИ > 100 баллов ЕГЭ > 100 за ДВИ."""
    kinds = {r.benefit for r in rules}
    return next((k for k in BENEFIT_TYPES if k in kinds), None)


def levels_label(levels: list[int] | None) -> str:
    if not levels or set(levels) == {1, 2, 3}:
        return ""
    lv = sorted(levels)
    return ROMAN[lv[0]] + (f"–{ROMAN[lv[-1]]}" if len(lv) > 1 else "") + " уровень"


def strongest_routes(benefits: BenefitsCatalog, catalog: Catalog, limit: int = 6) -> list[dict]:
    """«Где диплом сильнее всего»: пары олимпиада + профиль, по которым больше всего вузов дают БВИ призёрам."""
    bvi: dict[tuple[str, str], set[str]] = {}
    score: dict[tuple[str, str], set[str]] = {}
    for u in benefits.latest():
        for r in u.rules:
            if r.olympiad.is_vsosh or r.diploma == "winner" or not r.profiles:
                continue
            target = bvi if r.benefit == "bvi" else score
            for p in r.profiles:
                target.setdefault((r.olympiad.id, p), set()).add(u.university)
    ranked = sorted(bvi, key=lambda k: (-len(bvi[k]), -len(score.get(k, set()) - bvi[k]), k[1]))
    # по одной олимпиаде на профиль — иначе весь список займёт математика
    keys, seen_profiles, seen_olympiads = [], set(), set()
    for o, p in ranked:
        if p in seen_profiles or o in seen_olympiads:
            continue
        keys.append((o, p))
        seen_profiles.add(p)
        seen_olympiads.add(o)
        if len(keys) == limit:
            break
    total = len(benefits.latest()) or 1
    return [{"olympiad": catalog.get(o), "profile": p, "bvi": len(bvi[(o, p)]),
             "score": len(score.get((o, p), set()) - bvi[(o, p)]), "total": total} for o, p in keys]


def create_app(catalog: Catalog, benefits: BenefitsCatalog, *, storage_path: Path | str | None = None,
               today: Callable[[], date] = date.today, secret: str = "", admin_id: int = 0,
               site_url: str = "", crm_key: str = "") -> FastAPI:
    app = FastAPI(title="Бвишечка", docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/assets", StaticFiles(directory=HERE.parent / "assets"), name="assets")
    templates = Jinja2Templates(directory=HERE / "templates")
    admission_year = int(catalog.current_year[:4]) + 1
    universities = benefits.latest()
    by_slug = {slug(u.university): u for u in universities}
    uni_slug = {u.university: s for s, u in by_slug.items()}
    # диплом действует 4 года, следующих за годом олимпиады: на приём 2027 годятся дипломы с 2022/23
    diploma_years = catalog.years[:5]
    secret = secret or secrets.token_hex(16)
    olympiad_count = sum(1 for o in catalog.olympiads.values() if o.profiles_in(catalog.current_year) and not o.is_vsosh)
    routes = strongest_routes(benefits, catalog)

    # Своя база для тестов (в памяти, одна на приложение) или база бота — новое подключение на запрос
    shared = Storage(":memory:") if storage_path is None else None
    lock = threading.Lock()

    @contextmanager
    def db() -> Iterator[Storage]:
        if shared is not None:
            with lock:
                yield shared
            return
        storage = Storage(storage_path)
        try:
            yield storage
        finally:
            storage.close()

    app.state.db = db  # для тестов и служебных скриптов

    from app import stats as stats_mod

    VISITOR_COOKIE = "bv_vid"
    SKIP_PREFIXES = ("/static", "/assets", "/api", "/admin", "/login/check", "/news/rss.xml", "/favicon")

    @app.middleware("http")
    async def track_pageviews(request: Request, call_next):
        """Просмотры страниц для статистики: обезличенный id браузера, источник (utm, реферер), устройство.
        Заодно продлевает cookie входа: 30 дней считаются с последнего захода, а не со входа."""
        response = await call_next(request)
        path = request.url.path
        sid = request.cookies.get(SESSION_COOKIE)
        if sid and "set-cookie" not in response.headers and response.status_code < 400 \
                and not path.startswith(("/static", "/assets")):
            response.set_cookie(SESSION_COOKIE, sid, max_age=60 * 60 * 24 * SESSION_DAYS, httponly=True,
                                samesite="lax", secure=request.url.scheme == "https")
        ua = request.headers.get("user-agent", "")
        if (request.method != "GET" or response.status_code != 200 or path.startswith(SKIP_PREFIXES)
                or not response.headers.get("content-type", "").startswith("text/html") or stats_mod.is_bot(ua)):
            return response
        try:
            visitor = request.cookies.get(VISITOR_COOKIE, "")
            new = not re.fullmatch(r"[\w-]{16,40}", visitor)
            if new:
                visitor = secrets.token_urlsafe(16)
            user = current_user(request)
            if admin_id and user == admin_id:
                return response          # свои заходы не считаем
            ref = request.headers.get("referer", "")
            ref_host = re.sub(r"^https?://([^/:]+).*$", r"\1", ref) if ref.startswith("http") else ""
            if ref_host and ref_host == request.url.hostname:
                ref_host = ""
            qp = request.query_params
            with db() as s:
                s.log_pageview(visitor=visitor, telegram_id=user, path=path, ref_host=ref_host,
                               utm_source=qp.get("utm_source", ""), utm_campaign=qp.get("utm_campaign", ""),
                               device=stats_mod.device_of(ua), day=today().isoformat())
            if new:
                response.set_cookie(VISITOR_COOKIE, visitor, max_age=60 * 60 * 24 * 365, httponly=True,
                                    samesite="lax", secure=request.url.scheme == "https")
        except Exception:  # noqa: BLE001 — статистика не должна ронять сайт
            pass
        return response

    def short(o: CatalogOlympiad) -> str:
        from app.bot.texts import button_name

        return "ВсОШ" if o.is_vsosh else button_name(o, 70)

    def current_user(request: Request) -> int | None:
        sid = request.cookies.get(SESSION_COOKIE)
        if not sid:
            return None
        with db() as s:
            return s.session_user(sid)

    def csrf_token(request: Request) -> str:
        sid = request.cookies.get(SESSION_COOKIE, "")
        return hmac.new(secret.encode(), sid.encode(), hashlib.sha256).hexdigest()[:32]

    def check_csrf(request: Request, token: str) -> int:
        """Для POST из кабинета: пользователь вошёл и форма пришла с нашего сайта."""
        user = current_user(request)
        if user is None:
            raise HTTPException(401, "Войди, чтобы сохранить")
        if not hmac.compare_digest(token, csrf_token(request)):
            raise HTTPException(403, "Форма устарела — обнови страницу")
        return user

    def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
        user = current_user(request)
        name = ""
        pending_access = pending_news = pending_unis = 0
        if user:
            with db() as s:
                name = s.profile(user)["name"]
                if admin_id and user == admin_id:
                    pending_access = len(s.crm_accesses(status="pending"))
                    pending_news = s.news_count("draft")
                    pending_unis = len(s.uni_requests("new"))
        base = dict(
            request=request, bot_url=BOT_URL, admission_year=admission_year, current_year=catalog.current_year,
            benefit_types=BENEFIT_TYPES, diplomas=DIPLOMAS, uni_slug=uni_slug, levels_label=levels_label,
            olympiad_count=olympiad_count, university_count=len(universities), user=user, user_name=name,
            csrf=csrf_token(request) if user else "", short=short, diploma_years=diploma_years,
            is_admin=bool(admin_id) and user == admin_id, pending_access=pending_access,
            pending_news=pending_news, pending_unis=pending_unis,
        )
        return templates.TemplateResponse(request, template, {**base, **context}, status_code=status_code)

    def get_olympiad(olympiad_id: str) -> CatalogOlympiad:
        o = catalog.get(olympiad_id)
        if o is None:
            raise HTTPException(404, "Олимпиада не найдена")
        return o

    def back(request: Request, fallback: str) -> RedirectResponse:
        """После POST — туда, откуда пришли (только наш сайт)."""
        target = request.headers.get("referer", "")
        path = re.sub(r"^https?://[^/]+", "", target) if target.startswith(str(request.base_url)) else ""
        return RedirectResponse(path or fallback, status_code=303)

    def fit_for(o: CatalogOlympiad, profile: str, diploma: str, grade: int | None, year: str):
        level, level_year = profile_level(o, profile, year)
        return level, level_year, fitting(benefits, o.id, profile, diploma, grade, level)

    # -- страницы --------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        popular = [h.olympiad for q in ("Высшая проба", "Ломоносов", "Физтех", "Московская олимпиада школьников",
                                        "СПбГУ", "НТО") for h in catalog.search(q, limit=1)]
        uni_counts = [(u, len({r.olympiad.id for r in u.rules}), sum(r.benefit == "bvi" for r in u.rules))
                      for u in universities]
        with db() as s:
            latest_news = s.news_list("published", limit=3)
        return render(request, "index.html", popular=list({o.id: o for o in popular}.values()),
                      routes=routes, uni_counts=uni_counts, latest_news=[news_view(i) for i in latest_news])

    @app.get("/olympiads", response_class=HTMLResponse)
    def olympiads(request: Request, q: str = "", level: int = Query(0, ge=0, le=3)):
        year = catalog.current_year
        q = " ".join(q.split())[:100]
        if q:
            hits = catalog.search(q, limit=60)
            with db() as s:
                user = current_user(request)
                if not (admin_id and user == admin_id):
                    s.log_search("site", q, len(hits), user)
            if len(hits) == 1:
                return RedirectResponse(f"/olympiad/{hits[0].olympiad.id}", status_code=303)
            items = [(h.olympiad, h.matched_profiles) for h in hits]
        else:
            items = []
            for o in catalog.olympiads.values():
                profiles = o.profiles_in(year)
                if o.is_vsosh or not profiles:
                    continue
                chosen = sorted(p for p, v in profiles.items() if not level or v.level == level)
                if chosen:
                    items.append((o, chosen))
            from app.bot.texts import sort_key

            items.sort(key=lambda item: sort_key(short(item[0])))
        rows = [(o, matched, o.best_level(year), len(benefits.for_olympiad(o.id))) for o, matched in items]
        return render(request, "olympiads.html", q=q, level=level, rows=rows)

    @app.get("/olympiad/{olympiad_id}", response_class=HTMLResponse)
    def olympiad(request: Request, olympiad_id: str, profile: str = "", diploma: str = "", grade: int = 0,
                 year: str = ""):
        o = get_olympiad(olympiad_id)
        if o.id != olympiad_id:  # старый id переименованной олимпиады
            return RedirectResponse(f"/olympiad/{o.id}", status_code=301)
        from app.bot.texts import PRIZE_BVI_WARNING, not_in_list_banner, prize_bvi_affected

        entries = benefits.for_olympiad(o.id)
        all_rules = [r for _, rs in entries for r in rs]
        profiles = olympiad_profiles(benefits, o.id)
        year = year if year in diploma_years else catalog.current_year
        fit = None
        if profile in profiles and diploma in DIPLOMA_CHOICES.values():
            level, level_year, found = fit_for(o, profile, diploma, grade or None, year)
            fit = dict(profile=profile, diploma=diploma, grade=grade or None, year=year, level=level,
                       level_year=level_year, entries=[(u, summarize(rs), best_benefit(rs)) for u, rs in found])
            fit["entries"].sort(key=lambda e: (list(BENEFIT_TYPES).index(e[2]), e[0].university))
            fit["bvi"] = sum(e[2] == "bvi" for e in fit["entries"])
        year_rows = [(name, [o.profiles[name][y].level if y in o.profiles[name] else None for y in diploma_years])
                     for name in sorted(o.profiles) if any(y in o.profiles[name] for y in diploma_years)]
        user = current_user(request)
        with db() as s:
            stages = s.upcoming_stages(o.id, today(), limit=8)
            following = bool(user) and any(catalog.canonical_id(x) == o.id for x in s.subscriptions(user))
        return render(
            request, "olympiad.html", o=o, name=short(o), entries=[(u, summarize(rs)) for u, rs in entries],
            profiles=profiles, fit=fit, form=dict(profile=profile, diploma=diploma, grade=grade, year=year),
            stages=stages, today=today(), years=diploma_years, year_rows=year_rows, following=following,
            prize_warning=plain(PRIZE_BVI_WARNING) if prize_bvi_affected(all_rules) else "",
            notes=[plain(n) for n in not_in_list_banner(o, all_rules, admission_year) if plain(n)],
        )

    @app.get("/olympiad/{olympiad_id}/pdf")
    def olympiad_pdf(olympiad_id: str, profile: str = "", diploma: str = "", grade: int = 0, year: str = ""):
        from app.bot.texts import PRIZE_BVI_WARNING, not_in_list_banner, prize_bvi_affected
        from app.export_pdf import benefits_pdf, pdf_filename

        o = get_olympiad(olympiad_id)
        if profile not in olympiad_profiles(benefits, o.id):
            return RedirectResponse(f"/olympiad/{o.id}#fit", status_code=303)
        if diploma in DIPLOMA_CHOICES.values():
            year = year if year in diploma_years else catalog.current_year
            level, level_year, entries = fit_for(o, profile, diploma, grade or None, year)
            parts = [f"Профиль «{profile}»", "победитель" if diploma == "winner" else "призёр"]
            parts += [f"{grade} класс"] if grade else []
            parts += [f"уровень {level} в перечне {level_year}"] if level and not o.is_vsosh else []
            subtitle = " · ".join(parts)
        else:
            entries = [(u, [r for r in rs if not r.profiles or profile in r.profiles])
                       for u, rs in benefits.for_olympiad(o.id)]
            entries = [(u, rs) for u, rs in entries if rs]
            subtitle = f"Профиль «{profile}» · победители и призёры · все вузы"
        rules = [r for _, rs in entries for r in rs]
        notes = ([PRIZE_BVI_WARNING] if prize_bvi_affected(rules) else []) + not_in_list_banner(
            o, rules, admission_year)
        data = benefits_pdf(o, entries, admission_year, subtitle=subtitle, notes=notes, today=today())
        return Response(data, media_type="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="{pdf_filename(o, slug(profile))}"'})

    @app.get("/olympiad/{olympiad_id}/{university_slug}", response_class=HTMLResponse)
    def pair(request: Request, olympiad_id: str, university_slug: str):
        o = get_olympiad(olympiad_id)
        u = by_slug.get(university_slug)
        if u is None:
            raise HTTPException(404, "Вуз не найден")
        rules = [r for r in u.rules if r.olympiad.id == o.id]
        if not rules:
            raise HTTPException(404, "Этот вуз не даёт льгот по этой олимпиаде")
        order = sorted(rules, key=lambda r: (list(BENEFIT_TYPES).index(r.benefit), (r.profiles or [""])[0]))
        return render(request, "pair.html", o=o, name=short(o), u=u, rules=order)

    @app.get("/universities", response_class=HTMLResponse)
    def university_list(request: Request):
        rows = []
        for u in universities:
            counts = Counter(r.benefit for r in u.rules)
            rows.append((u, counts, len({r.olympiad.id for r in u.rules})))
        user = current_user(request)
        following = set()
        if user:
            with db() as s:
                following = set(s.university_subscriptions(user))
        return render(request, "universities.html", rows=rows, following=following,
                      suggested=request.query_params.get("suggested") == "1")

    @app.get("/university/{university_slug}", response_class=HTMLResponse)
    def university(request: Request, university_slug: str):
        u = by_slug.get(university_slug)
        if u is None:
            raise HTTPException(404, "Вуз не найден")
        by_olympiad: dict[str, list[Rule]] = {}
        for r in u.rules:
            by_olympiad.setdefault(r.olympiad.id, []).append(r)
        from app.bot.texts import sort_key

        items = sorted(((catalog.get(oid), summarize(rs)) for oid, rs in by_olympiad.items()),
                       key=lambda item: (not item[0].is_vsosh, sort_key(short(item[0]))))
        user = current_user(request)
        with db() as s:
            following = bool(user) and s.is_subscribed_university(user, u.university)
        return render(request, "university.html", u=u, items=items, following=following,
                      u_slug=university_slug)

    # -- подбор от вуза --------------------------------------------------------

    from app.routes import RouteItem, program_label, program_names, route as build_route, university_programs

    names = program_names(benefits)

    @app.get("/route", response_class=HTMLResponse)
    def route_start(request: Request):
        rows = [(u, len(university_programs(u))) for u in universities]
        return render(request, "route_start.html", rows=rows)

    @app.get("/route/{university_slug}", response_class=HTMLResponse)
    def route_page(request: Request, university_slug: str, program: str = "", diploma: str = "prize",
                   profile: str = ""):
        u = by_slug.get(university_slug)
        if u is None:
            raise HTTPException(404, "Вуз не найден")
        programs = [(p, program_label(p, names)) for p in university_programs(u)]
        if program not in {p for p, _ in programs}:
            program = ""
        diploma = diploma if diploma in DIPLOMA_CHOICES.values() else "prize"
        year = catalog.current_year
        items = build_route(u, program, diploma, year) if program or not programs else []
        all_profiles = sorted({p.profile for it in items for p in it.profiles})
        if profile in all_profiles:
            items = [RouteItem(it.olympiad, [p for p in it.profiles if p.profile == profile]) for it in items]
            items = sorted((it for it in items if it.profiles), key=lambda it: (
                not it.olympiad.is_vsosh, list(BENEFIT_TYPES).index(it.best), it.olympiad.name))
        else:
            profile = ""
        with db() as s:
            stages = {it.olympiad.id: s.upcoming_stages(it.olympiad.id, today(), limit=1) for it in items}
        counts = Counter(it.best for it in items)
        return render(request, "route.html", u=u, u_slug=university_slug, programs=programs, program=program,
                      program_title=program_label(program, names), diploma=diploma, profile=profile,
                      all_profiles=all_profiles, items=items, stages=stages, counts=counts, year=year,
                      today=today(), chosen=bool(program or not programs))

    # -- сопровождение ---------------------------------------------------------

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request, sent: int = 0):
        user = current_user(request)
        prefill = {"name": "", "grade": None}
        already = False
        if user:
            with db() as s:
                prefill = s.profile(user)
                already = s.recent_help_request(telegram_id=user)
        return render(request, "help.html", sent=bool(sent), prefill=prefill, already=already, error="")

    @app.post("/help", response_class=HTMLResponse)
    def help_submit(request: Request, csrf: str = Form(""), name: str = Form(""), contact: str = Form(""),
                    grade: int = Form(0), goal: str = Form(""), consent: str = Form(""), website: str = Form("")):
        user = check_csrf(request, csrf) if current_user(request) else None
        name, contact, goal = name.strip()[:100], contact.strip()[:200], goal.strip()[:2000]
        prefill = {"name": name, "grade": grade or None}

        def again(error: str):
            return render(request, "help.html", 400, sent=False, prefill=prefill, already=False, error=error,
                          form=dict(contact=contact, goal=goal))

        if website:  # поле-ловушка: человек его не видит, бот заполняет
            return RedirectResponse("/help?sent=1", status_code=303)
        if not consent:
            return again("Нужно согласие на обработку данных — иначе мы не сможем с тобой связаться.")
        if not user and len(contact) < 3:
            return again("Оставь контакт: ник в Telegram (@…), телефон или почту.")
        with db() as s:
            if s.recent_help_request(telegram_id=user, contact=contact):
                return RedirectResponse("/help?sent=1", status_code=303)
            if s.help_requests_since(10) >= 30:  # защита от заливки формы
                return again("Слишком много заявок за последние минуты. Попробуй чуть позже или напиши в бот.")
            s.add_help_request(source="site", telegram_id=user, name=name, contact=contact,
                               grade=grade if 7 <= grade <= 11 else None, goal=goal)
        return RedirectResponse("/help?sent=1", status_code=303)

    # -- вход через бота -------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login(request: Request):
        if current_user(request):
            return RedirectResponse("/me", status_code=303)
        token = secrets.token_urlsafe(18)
        with db() as s:
            s.create_login(token)
        return render(request, "login.html", token=token, link=f"{BOT_URL}?start=login_{token}")

    @app.get("/login/check")
    def login_check(token: str):
        """Страница входа опрашивает: подтвердил ли бот вход."""
        with db() as s:
            row = s.conn.execute("SELECT telegram_id FROM web_login WHERE token = ?", (token,)).fetchone()
        return JSONResponse({"ok": bool(row and row[0])})

    @app.get("/login/finish")
    def login_finish(request: Request, token: str):
        with db() as s:
            telegram_id = s.take_login(token)
            if telegram_id is None:
                return RedirectResponse("/login", status_code=303)
            sid = secrets.token_urlsafe(32)
            s.create_session(sid, telegram_id)
            s.log_auth(telegram_id, request.cookies.get("bv_vid", ""))
        response = RedirectResponse("/me", status_code=303)
        response.set_cookie(SESSION_COOKIE, sid, max_age=60 * 60 * 24 * SESSION_DAYS, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https")
        return response

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form("")):
        check_csrf(request, csrf)
        with db() as s:
            s.delete_session(request.cookies.get(SESSION_COOKIE, ""))
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    # -- личный кабинет --------------------------------------------------------

    @app.get("/me", response_class=HTMLResponse)
    def me(request: Request):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        with db() as s:
            profile = s.profile(user)
            raw_diplomas = s.diplomas(user)
            followed_ids = list(dict.fromkeys(catalog.canonical_id(x) for x in s.subscriptions(user)))
            followed_unis = s.university_subscriptions(user)
            stages = sorted((st for oid in followed_ids for st in s.upcoming_stages(oid, today(), limit=4)),
                            key=lambda st: st.starts_at or date.max)
            code = s.user_code(user)
            has_roadmap = s.crm_client(user) is not None
            phone = s.user_info(user)["phone"] or ""
        diplomas = []
        best_by_uni: dict[str, str] = {}
        for d in raw_diplomas:
            o = catalog.get(d["olympiad_id"])
            if o is None:
                continue
            level, level_year, found = fit_for(o, d["profile"], d["diploma"], d["grade"], d["academic_year"])
            matches = [(u, best_benefit(rs)) for u, rs in found]
            for u, kind in matches:
                current = best_by_uni.get(u.university)
                if current is None or list(BENEFIT_TYPES).index(kind) < list(BENEFIT_TYPES).index(current):
                    best_by_uni[u.university] = kind
            diplomas.append(dict(d, olympiad=o, level=level, matches=matches,
                                 bvi=sum(k == "bvi" for _, k in matches)))
        targets = [(by_slug[uni_slug[name]], best_by_uni.get(name)) for name in followed_unis if name in uni_slug]
        olympiads_followed = [catalog.get(oid) for oid in followed_ids if catalog.get(oid)]
        next_stage = {}
        for st in stages:
            next_stage.setdefault(catalog.canonical_id(st.olympiad_id), st)
        return render(request, "me.html", profile=profile, diplomas=diplomas, targets=targets,
                      olympiads_followed=olympiads_followed, next_stage=next_stage, stages=stages[:12],
                      today=today(), best_by_uni=best_by_uni, all_universities=universities, code=code,
                      has_roadmap=has_roadmap, phone=phone)

    @app.post("/me/profile")
    def me_profile(request: Request, csrf: str = Form(""), name: str = Form(""), grade: int = Form(0),
                   phone: str = Form("")):
        user = check_csrf(request, csrf)
        digits = re.sub(r"\D", "", phone)
        if digits.startswith("8") and len(digits) == 11:
            digits = "7" + digits[1:]
        with db() as s:
            s.save_profile(user, " ".join(name.split())[:60], grade if 5 <= grade <= 11 else None)
            if not phone.strip():
                s.set_phone(user, None)
            elif 10 <= len(digits) <= 15:
                s.set_phone(user, "+" + digits)
        return RedirectResponse("/me#settings", status_code=303)

    @app.get("/me/diploma/new", response_class=HTMLResponse)
    def me_new_diploma(request: Request, q: str = "", olympiad: str = ""):
        """Добавить диплом: найти олимпиаду → профиль, победитель/призёр, класс, год."""
        if current_user(request) is None:
            return RedirectResponse("/login", status_code=303)
        chosen = catalog.get(olympiad) if olympiad else None
        hits = []
        q = " ".join(q.split())[:100]
        if chosen is None and q:
            hits = [h.olympiad for h in catalog.search(q, limit=20)]
            if len(hits) == 1:
                chosen = hits[0]
        profiles = sorted(set(olympiad_profiles(benefits, chosen.id)) | set(chosen.profiles)) if chosen else []
        return render(request, "diploma_new.html", q=q, hits=hits, chosen=chosen, profiles=profiles)

    @app.post("/me/diplomas")
    def me_add_diploma(request: Request, csrf: str = Form(""), olympiad_id: str = Form(...),
                       profile: str = Form(...), diploma: str = Form(...), grade: int = Form(11),
                       year: str = Form("")):
        user = check_csrf(request, csrf)
        o = get_olympiad(olympiad_id)
        if profile not in olympiad_profiles(benefits, o.id) and profile not in o.profiles:
            raise HTTPException(422, "У олимпиады нет такого профиля")
        if diploma not in DIPLOMA_CHOICES.values() or not 5 <= grade <= 11:
            raise HTTPException(422, "Неверный диплом или класс")
        with db() as s:
            s.add_diploma(user, o.id, profile, diploma, grade, year if year in diploma_years else catalog.current_year)
            if not any(catalog.canonical_id(x) == o.id for x in s.subscriptions(user)):
                s.subscribe(user, o.id)  # следим за олимпиадой, раз в ней участвуем
        return RedirectResponse("/me#diplomas", status_code=303)

    @app.post("/me/diplomas/{diploma_id}/delete")
    def me_delete_diploma(request: Request, diploma_id: int, csrf: str = Form("")):
        user = check_csrf(request, csrf)
        with db() as s:
            s.delete_diploma(user, diploma_id)
        return RedirectResponse("/me#diplomas", status_code=303)

    @app.post("/me/olympiads/{olympiad_id}")
    def me_follow_olympiad(request: Request, olympiad_id: str, csrf: str = Form(""), action: str = Form("follow")):
        user = check_csrf(request, csrf)
        o = get_olympiad(olympiad_id)
        with db() as s:
            if action == "follow":
                s.subscribe(user, o.id)
            else:
                for sid in s.subscriptions(user):
                    if catalog.canonical_id(sid) == o.id:
                        s.unsubscribe(user, sid)
        return back(request, f"/olympiad/{o.id}")

    @app.post("/me/universities")
    def me_add_university(request: Request, csrf: str = Form(""), university: str = Form("")):
        """Добавить вуз в цели прямо из кабинета (список вузов)."""
        user = check_csrf(request, csrf)
        u = benefits.get(university)
        if u is None:
            raise HTTPException(404, "Вуз не найден")
        with db() as s:
            s.subscribe_university(user, u.university)
        return RedirectResponse("/me#targets", status_code=303)

    @app.post("/me/universities/{university_slug}")
    def me_follow_university(request: Request, university_slug: str, csrf: str = Form(""),
                             action: str = Form("follow")):
        user = check_csrf(request, csrf)
        u = by_slug.get(university_slug)
        if u is None:
            raise HTTPException(404, "Вуз не найден")
        with db() as s:
            if action == "follow":
                s.subscribe_university(user, u.university)
            else:
                s.unsubscribe_university(user, u.university)
        return back(request, f"/university/{university_slug}")

    @app.post("/me/delete")
    def me_delete(request: Request, csrf: str = Form(""), confirm: str = Form("")):
        user = check_csrf(request, csrf)
        if confirm != "удалить":
            return RedirectResponse("/me#settings", status_code=303)
        with db() as s:
            s.delete_user_data(user)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    # -- сопровождение: роадмап клиента и админка куратора ----------------------

    from app import crm

    from app.secretbox import SecretBox

    box = SecretBox(crm_key)

    def roadmap_for(s: Storage, user: int) -> crm.Roadmap:
        accesses = {a["crm_olympiad"]: a["status"] for a in s.crm_accesses(user)}
        rm = crm.build_roadmap(catalog, benefits, s.crm_goals(user), s.crm_olympiads(user), s.stages, today(),
                               accesses)
        for it in rm.items:
            if it.plan_changed is not None:
                s.crm_save_plan(it.row_id, it.plan_changed)   # прошедшие этапы не теряются
        return rm

    def upcoming_for(s: Storage, rm: crm.Roadmap, limit: int = 6) -> list[tuple[date, str, str]]:
        """Ближайшие даты плана: (дата, что, олимпиада) — конец регистрации и этапы с сайтов олимпиад."""
        found: list[tuple[date, str, str]] = []
        for it in rm.items:
            name = short(it.olympiad)
            if it.status in crm.DEADLINE_STATUSES and it.deadline and it.deadline >= today():
                found.append((it.deadline, "конец регистрации", name))
            if it.needs_access and it.access_deadline and it.access_deadline >= today():
                found.append((it.access_deadline, "ввести доступ к кабинету", name))
            if it.status_key not in crm.RESULT_STATUSES and not it.status_key.startswith("f"):
                for st in s.upcoming_stages(it.olympiad.id, today(), limit=4):
                    if st.kind != "registration" and crm.stage_fits(st.profile, it.profile):
                        found.append((st.starts_at or st.ends_at, st.title or st.kind_label, name))
        return sorted({f for f in found if f[0]})[:limit]

    def roadmap_page(request: Request, user: int, *, shared: bool):
        with db() as s:
            client = s.crm_client(user)
            if client is None:
                return None
            rm = roadmap_for(s, user)
            upcoming = upcoming_for(s, rm)
        return render(request, "roadmap.html", client=client, rm=rm, upcoming=upcoming, shared=shared,
                      today=today(), benefit_short=crm.BENEFIT_SHORT, site_url=site_url,
                      access_labels=crm.ACCESS_LABELS, access_enabled=box.enabled,
                      access_error=request.query_params.get("access_error", ""))

    @app.get("/me/roadmap", response_class=HTMLResponse)
    def me_roadmap(request: Request):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        return roadmap_page(request, user, shared=False) or RedirectResponse("/me", status_code=303)

    @app.get("/r/{token}", response_class=HTMLResponse)
    def shared_roadmap(request: Request, token: str):
        """Ссылка для родителей: только просмотр, без входа."""
        with db() as s:
            user = s.crm_client_by_token(token)
        page = roadmap_page(request, user, shared=True) if user else None
        if page is None:
            raise HTTPException(404, "Ссылка не найдена")
        page.headers["X-Robots-Tag"] = "noindex"
        return page

    @app.post("/me/roadmap/{row_id}/access")
    def me_save_access(request: Request, row_id: int, csrf: str = Form(""), login: str = Form(""),
                       password: str = Form(""), consent: str = Form("")):
        """Клиент вводит логин и пароль от кабинета олимпиады — они уходят куратору на проверку."""
        user = check_csrf(request, csrf)
        login, password = login.strip()[:200], password.strip()[:200]
        if not box.enabled:
            raise HTTPException(503, "Приём доступов пока не настроен")
        if not login or not password or not consent:
            return RedirectResponse(f"/me/roadmap?access_error={row_id}#o{row_id}", status_code=303)
        with db() as s:
            if s.crm_client(user) is None or s.crm_olympiad(user, row_id) is None:
                raise HTTPException(404, "Олимпиада не найдена")
            s.crm_save_access(user, row_id, box.seal(login), box.seal(password))
            row = s.crm_olympiad(user, row_id)
            o = catalog.get(row["olympiad_id"])
            s.crm_log(user, f"Клиент ввёл доступ: {short(o) if o else row['olympiad_id']}")
        return RedirectResponse(f"/me/roadmap#o{row_id}", status_code=303)

    @app.post("/me/roadmap/{row_id}/access/delete")
    def me_delete_access(request: Request, row_id: int, csrf: str = Form("")):
        user = check_csrf(request, csrf)
        with db() as s:
            s.crm_delete_access(user, row_id)
            if s.crm_client(user):
                s.crm_log(user, "Клиент удалил доступ к кабинету")
        return RedirectResponse(f"/me/roadmap#o{row_id}", status_code=303)

    def require_admin(request: Request) -> int:
        user = current_user(request)
        if not admin_id or user != admin_id:
            raise HTTPException(404, "Такой страницы нет")
        return user

    def admin_post(request: Request, csrf: str) -> None:
        if check_csrf(request, csrf) != admin_id or not admin_id:
            raise HTTPException(404, "Такой страницы нет")

    def client_by_code(s: Storage, code: str) -> int:
        user = s.user_by_code(code)
        if user is None or s.crm_client(user) is None:
            raise HTTPException(404, "Клиент не найден")
        return user

    def notify(s: Storage, user: int, text: str) -> None:
        client = s.crm_client(user)
        if client and client["notify"]:
            s.queue_message(user, text)

    def attention(rm: crm.Roadmap) -> list[str]:
        """Что требует внимания куратора по клиенту."""
        out = []
        for it in rm.items:
            if it.status not in crm.DEADLINE_STATUSES:
                continue
            if it.deadline and 0 <= (it.deadline - today()).days <= 7:
                out.append(f"регистрация на {short(it.olympiad)} до {it.deadline:%d.%m}")
            elif it.deadline is None and it.status == "registration":
                out.append(f"нет даты регистрации: {short(it.olympiad)}")
            if it.access == "pending":
                out.append(f"проверить доступ: {short(it.olympiad)}")
            elif it.needs_access and it.access_deadline and (it.access_deadline - today()).days <= 3:
                out.append(f"нет доступа к кабинету: {short(it.olympiad)}")
        return out

    @app.get("/admin", response_class=HTMLResponse)
    def admin_clients(request: Request, group: int = 0, q: str = "", error: str = ""):
        require_admin(request)
        q = " ".join(q.split()).lower()[:60]
        rows = []
        with db() as s:
            groups = s.crm_groups()
            for c in s.crm_clients(group or None):
                if q and q not in (c["name"] or "").lower() and q.replace(" ", "") not in (c["code"] or ""):
                    continue
                rm = roadmap_for(s, c["telegram_id"])
                rows.append(dict(c, rm=rm, goals=[g.university for g in rm.goals], attention=attention(rm),
                                 upcoming=upcoming_for(s, rm, limit=1)))
            total = len(s.crm_clients())
        return render(request, "admin.html", rows=rows, groups=groups, group=group, q=q, total=total,
                      error="Нет пользователя с таким ID — пусть клиент войдёт на сайт и пришлёт ID из кабинета"
                      if error == "nouser" else "", attention_total=sum(len(r["attention"]) for r in rows))

    @app.post("/admin/open")
    def admin_open(request: Request, csrf: str = Form(""), code: str = Form("")):
        admin_post(request, csrf)
        with db() as s:
            user = s.user_by_code(code)
            if user is None:
                return RedirectResponse("/admin?error=nouser", status_code=303)
            if s.crm_enable(user):
                s.crm_log(user, "Сопровождение включено")
            real = s.user_code(user)
        return RedirectResponse(f"/admin/client/{real}", status_code=303)

    @app.post("/admin/groups")
    def admin_add_group(request: Request, csrf: str = Form(""), name: str = Form("")):
        admin_post(request, csrf)
        name = " ".join(name.split())[:60]
        if name:
            with db() as s:
                gid = s.crm_add_group(name)
            return RedirectResponse(f"/admin?group={gid}", status_code=303)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/groups/{group_id}/delete")
    def admin_delete_group(request: Request, group_id: int, csrf: str = Form("")):
        admin_post(request, csrf)
        with db() as s:
            s.crm_delete_group(group_id)
        return RedirectResponse("/admin", status_code=303)

    @app.get("/admin/client/{code}", response_class=HTMLResponse)
    def admin_client(request: Request, code: str, q: str = ""):
        require_admin(request)
        q = " ".join(q.split())[:100]
        with db() as s:
            user = client_by_code(s, code)
            client = s.crm_client(user)
            goals = s.crm_goals(user)
            rm = roadmap_for(s, user)
            events = s.crm_events(user)
            groups = s.crm_groups()
        taken = {(it.olympiad.id, it.profile) for it in rm.items}
        hits = []
        if q:
            year = catalog.current_year
            for h in catalog.search(q, limit=10):
                profiles = sorted(h.olympiad.profiles_in(year)) or sorted(h.olympiad.profiles)
                hits.append((h.olympiad, profiles))
        uni_programs = {u.university: [(p, program_label(p, names)) for p in university_programs(u)]
                        for u in universities}
        return render(request, "admin_client.html", client=client, rm=rm, events=events, groups=groups,
                      q=q, hits=hits, suggestions=crm.suggestions(catalog, benefits, goals, taken),
                      uni_programs=uni_programs, access_labels=crm.ACCESS_LABELS, benefit_short=crm.BENEFIT_SHORT,
                      site_url=site_url, today=today(), attention=attention(rm))

    @app.post("/admin/client/{code}/settings")
    def admin_client_settings(request: Request, code: str, csrf: str = Form(""), group_id: int = Form(0),
                              notes: str = Form(""), notify_on: str = Form("")):
        admin_post(request, csrf)
        with db() as s:
            user = client_by_code(s, code)
            valid = {g["id"] for g in s.crm_groups()}
            s.crm_update_client(user, group_id=group_id if group_id in valid else None, notes=notes[:4000],
                                notify=1 if notify_on else 0)
        return RedirectResponse(f"/admin/client/{code}", status_code=303)

    @app.post("/admin/client/{code}/goals")
    def admin_add_goal(request: Request, code: str, csrf: str = Form(""), university: str = Form(""),
                       program: str = Form("")):
        admin_post(request, csrf)
        u = benefits.get(university)
        if u is None:
            raise HTTPException(422, "Нет такого вуза")
        if program and program not in university_programs(u):
            program = ""
        with db() as s:
            user = client_by_code(s, code)
            if s.crm_add_goal(user, u.university, program):
                s.crm_log(user, f"Цель: {u.university}" + (f", {program_label(program, names)}" if program else ""))
        return RedirectResponse(f"/admin/client/{code}#goals", status_code=303)

    @app.post("/admin/client/{code}/goals/{goal_id}/delete")
    def admin_delete_goal(request: Request, code: str, goal_id: int, csrf: str = Form("")):
        admin_post(request, csrf)
        with db() as s:
            s.crm_delete_goal(client_by_code(s, code), goal_id)
        return RedirectResponse(f"/admin/client/{code}#goals", status_code=303)

    @app.post("/admin/client/{code}/olympiads")
    def admin_add_olympiad(request: Request, code: str, csrf: str = Form(""), olympiad_id: str = Form(""),
                           profile: str = Form(""), to_group: str = Form("")):
        admin_post(request, csrf)
        o = get_olympiad(catalog.canonical_id(olympiad_id))
        if profile and profile not in o.profiles:
            raise HTTPException(422, "У олимпиады нет такого профиля")
        with db() as s:
            user = client_by_code(s, code)
            client = s.crm_client(user)
            members = s.crm_group_members(client["group_id"]) if to_group and client["group_id"] else [user]
            name = short(o) + (f", {profile}" if profile else "")
            for member in members:
                if s.crm_add_olympiad(member, o.id, profile):
                    s.subscribe(member, o.id)   # обычные напоминания бота о регистрации и этапах
                    s.crm_log(member, f"Добавлена олимпиада: {name}")
                    notify(s, member, f"<b>В план добавлена олимпиада</b>\n{crm.escape(name)}. "
                                      "Напомним о регистрации и этапах.")
        return RedirectResponse(f"/admin/client/{code}#olympiads", status_code=303)

    @app.post("/admin/client/{code}/olympiads/{row_id}/status")
    def admin_set_status(request: Request, code: str, row_id: int, csrf: str = Form(""), status: str = Form("")):
        admin_post(request, csrf)
        if not crm.valid_status(status):
            raise HTTPException(422, "Нет такого статуса")
        with db() as s:
            user = client_by_code(s, code)
            row = s.crm_olympiad(user, row_id)
            if row is None:
                raise HTTPException(404, "Олимпиада не найдена")
            item = next((it for it in roadmap_for(s, user).items if it.row_id == row_id), None)
            n = item.n if item else 1
            if crm.normalize_status(row["status"], n) != status:
                s.crm_set_status(user, row_id, status)
                o = catalog.get(row["olympiad_id"])
                name = short(o) if o else row["olympiad_id"]
                s.crm_log(user, f"{name}: {crm.status_label(row['status'], n)} → {crm.status_label(status, n)}")
                notify(s, user, crm.status_message(name, row["profile"], status, n, site_url))
        return RedirectResponse(f"/admin/client/{code}#olympiads", status_code=303)

    @app.post("/admin/client/{code}/olympiads/{row_id}/deadline")
    def admin_set_deadline(request: Request, code: str, row_id: int, csrf: str = Form(""),
                           deadline: str = Form("")):
        admin_post(request, csrf)
        try:
            value = date.fromisoformat(deadline) if deadline else None
        except ValueError:
            raise HTTPException(422, "Дата в формате ГГГГ-ММ-ДД") from None
        with db() as s:
            s.crm_set_deadline(client_by_code(s, code), row_id, value)
        return RedirectResponse(f"/admin/client/{code}#olympiads", status_code=303)

    @app.post("/admin/client/{code}/olympiads/{row_id}/delete")
    def admin_delete_olympiad(request: Request, code: str, row_id: int, csrf: str = Form("")):
        admin_post(request, csrf)
        with db() as s:
            user = client_by_code(s, code)
            row = s.crm_olympiad(user, row_id)
            if row:
                s.crm_delete_olympiad(user, row_id)
                o = catalog.get(row["olympiad_id"])
                s.crm_log(user, f"Убрана олимпиада: {short(o) if o else row['olympiad_id']}")
        return RedirectResponse(f"/admin/client/{code}#olympiads", status_code=303)

    @app.post("/admin/client/{code}/message")
    def admin_message(request: Request, code: str, csrf: str = Form(""), text: str = Form("")):
        admin_post(request, csrf)
        text = text.strip()[:2000]
        if text:
            with db() as s:
                user = client_by_code(s, code)
                s.queue_message(user, "<b>Сообщение от куратора</b>\n" + crm.escape(text))
                s.crm_log(user, "Сообщение клиенту: " + text[:200])
        return RedirectResponse(f"/admin/client/{code}", status_code=303)

    @app.post("/admin/client/{code}/disable")
    def admin_disable(request: Request, code: str, csrf: str = Form(""), confirm: str = Form("")):
        admin_post(request, csrf)
        if confirm.strip().lower() != "выключить":
            return RedirectResponse(f"/admin/client/{code}#danger", status_code=303)
        with db() as s:
            s.crm_disable(client_by_code(s, code))
        return RedirectResponse("/admin", status_code=303)

    @app.get("/admin/access", response_class=HTMLResponse)
    def admin_access(request: Request, show: int = 0):
        """Очередь доступов на проверку. Пароль виден только после «Показать» (POST) — и это пишется в журнал."""
        require_admin(request)
        with db() as s:
            pending = s.crm_accesses(status="pending")
            log_rows = s.crm_access_log()
        items = []
        for a in pending:
            o = catalog.get(a["olympiad_id"])
            items.append(dict(a, olympiad=o, login=box.open(a["login_enc"]) if box.enabled else "—"))
        return render(request, "admin_access.html", items=items, log_rows=log_rows, enabled=box.enabled,
                      revealed={})

    @app.post("/admin/access/{access_id}/reveal", response_class=HTMLResponse)
    def admin_access_reveal(request: Request, access_id: int, csrf: str = Form("")):
        admin_post(request, csrf)
        with db() as s:
            a = s.crm_access(access_id)
            if a is None or not box.enabled:
                raise HTTPException(404, "Доступ не найден")
            o = catalog.get(a["olympiad_id"])
            s.crm_access_log_add(a["telegram_id"], f"просмотрен пароль: {a['name'] or a['code']}, "
                                                   f"{short(o) if o else a['olympiad_id']}")
            pending = s.crm_accesses(status="pending")
            log_rows = s.crm_access_log()
        items = [dict(x, olympiad=catalog.get(x["olympiad_id"]), login=box.open(x["login_enc"])) for x in pending]
        if a["status"] != "pending":
            items.insert(0, dict(a, olympiad=o, login=box.open(a["login_enc"])))
        response = render(request, "admin_access.html", items=items, log_rows=log_rows, enabled=True,
                          revealed={access_id: box.open(a["password_enc"])})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/admin/access/{access_id}/check")
    def admin_access_check(request: Request, access_id: int, csrf: str = Form(""), result: str = Form("")):
        admin_post(request, csrf)
        if result not in ("ok", "bad"):
            raise HTTPException(422, "ok или bad")
        with db() as s:
            a = s.crm_access(access_id)
            if a is None:
                raise HTTPException(404, "Доступ не найден")
            s.crm_set_access_status(access_id, result)
            o = catalog.get(a["olympiad_id"])
            name = short(o) if o else a["olympiad_id"]
            s.crm_access_log_add(a["telegram_id"], f"{'проверен' if result == 'ok' else 'не подошёл'}: "
                                                   f"{a['name'] or a['code']}, {name}")
            s.crm_log(a["telegram_id"], f"Доступ к кабинету {name}: {'работает' if result == 'ok' else 'не подошёл'}")
            notify(s, a["telegram_id"], crm.access_message(name, result == "ok", site_url))
        return RedirectResponse("/admin/access", status_code=303)

    # -- новости ---------------------------------------------------------------

    from app import news as news_mod

    def news_view(item: dict) -> dict:
        import json as _json

        o = catalog.get(item["olympiad_id"]) if item.get("olympiad_id") else None
        try:
            sources = _json.loads(item.get("sources") or "[]")
        except ValueError:
            sources = []
        if not sources and item.get("source_url") and news_mod.is_official(item["source_url"]):
            sources = [{"url": item["source_url"], "title": "Источник", "ok": True}]
        category = item.get("category") or news_mod.KIND_CATEGORY.get(item["kind"], "")
        return dict(item, olympiad=o, slug=news_mod.slugify(item["title"]), sources_list=sources,
                    category=category, category_label=news_mod.CATEGORIES.get(category, ""),
                    kind_label=news_mod.KINDS.get(item["kind"], ""),
                    date=(item["published_at"] or item["created_at"])[:10])

    def check_all(sources: list[dict]) -> list[dict]:
        """Проверить ссылки параллельно (каждая — до 8 секунд)."""
        from concurrent.futures import ThreadPoolExecutor

        if not sources:
            return []
        with ThreadPoolExecutor(max_workers=8) as pool:
            oks = list(pool.map(news_mod.check_url, [x["url"] for x in sources]))
        stamp = today().isoformat()
        return [dict(x, ok=ok, checked_at=stamp) for x, ok in zip(sources, oks)]

    @app.get("/news", response_class=HTMLResponse)
    def news_page(request: Request, page: int = Query(1, ge=1, le=500), mine: int = 0, category: str = ""):
        """Лента новостей; mine=1 — только по олимпиадам, за которыми следишь; category — раздел."""
        user = current_user(request)
        category = category if category in news_mod.CATEGORIES else ""
        with db() as s:
            ids = None
            if mine and user:
                ids = list(dict.fromkeys(catalog.canonical_id(x) for x in s.subscriptions(user)))
                ids += [r["olympiad_id"] for r in (s.crm_olympiads(user) if s.crm_client(user) else [])]
            items = s.news_list("published", limit=21, offset=(page - 1) * 20, olympiad_ids=ids, category=category)
        return render(request, "news.html", items=[news_view(i) for i in items[:20]], page=page,
                      has_next=len(items) > 20, mine=bool(mine and user), category=category,
                      categories=news_mod.CATEGORIES)

    @app.get("/news/rss.xml")
    def news_rss(request: Request):
        from xml.sax.saxutils import escape as x

        base = site_url or str(request.base_url).rstrip("/")
        with db() as s:
            items = s.news_list("published", limit=50)
        entries = "".join(
            f"<item><title>{x(i['title'])}</title><link>{base}/news/{i['id']}-{news_mod.slugify(i['title'])}</link>"
            f"<guid>{base}/news/{i['id']}</guid><pubDate>{x(i['published_at'] or '')}</pubDate>"
            f"<description>{x(i['body'][:500])}</description></item>" for i in items)
        xml = (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Бвишечка — новости '
               f'олимпиад</title><link>{base}/news</link><description>Регистрации, этапы и льготы</description>'
               f'{entries}</channel></rss>')
        return Response(xml, media_type="application/rss+xml")

    @app.get("/news/{news_id:int}-{news_slug}", response_class=HTMLResponse)
    @app.get("/news/{news_id:int}", response_class=HTMLResponse)
    def news_one(request: Request, news_id: int, news_slug: str = ""):
        with db() as s:
            item = s.news_item(news_id)
        if item is None or (item["status"] != "published" and current_user(request) != admin_id):
            raise HTTPException(404, "Новость не найдена")
        view = news_view(item)
        if news_slug != view["slug"]:
            return RedirectResponse(f"/news/{news_id}-{view['slug']}", status_code=301)
        return render(request, "news_item.html", item=view)

    @app.get("/admin/news", response_class=HTMLResponse)
    def admin_news(request: Request, status: str = "draft", category: str = ""):
        require_admin(request)
        status = status if status in ("draft", "published", "rejected") else "draft"
        category = category if category in news_mod.CATEGORIES else ""
        with db() as s:
            items = s.news_list(status, limit=300, category=category)
            counts = {k: s.news_count(k) for k in ("draft", "published", "rejected")}
        return render(request, "admin_news.html", items=[news_view(i) for i in items], status=status, counts=counts,
                      kinds=news_mod.KINDS, categories=news_mod.CATEGORIES, category=category)

    @app.post("/admin/news/bulk")
    async def admin_news_bulk(request: Request):
        form = await request.form()
        admin_post(request, str(form.get("csrf", "")))
        action = str(form.get("action", ""))
        target = {"publish": "published", "reject": "rejected", "draft": "draft"}.get(action)
        ids = [int(v) for v in form.getlist("ids") if str(v).isdigit()]
        with db() as s:
            for news_id in ids:
                if action == "delete":
                    s.delete_news(news_id)
                elif target:
                    s.update_news(news_id, status=target)
        return RedirectResponse(f"/admin/news?status={form.get('back', 'draft')}", status_code=303)

    def edit_page(request: Request, item: dict | None, *, errors: list[str] | None = None, checked: bool = False):
        return render(request, "admin_news_edit.html", item=item, categories=news_mod.CATEGORIES,
                      sources_text=news_mod.sources_text(item["sources_list"]) if item else "",
                      errors=errors or [], checked=checked)

    @app.get("/admin/news/new", response_class=HTMLResponse)
    @app.get("/admin/news/{news_id:int}/edit", response_class=HTMLResponse)
    def admin_news_edit(request: Request, news_id: int = 0, template: int = -1):
        require_admin(request)
        item = None
        if news_id:
            with db() as s:
                found = s.news_item(news_id)
            if found is None:
                raise HTTPException(404, "Новость не найдена")
            item = news_view(found)
        elif template >= 0:
            from app.news_seed import ARTICLES

            if template < len(ARTICLES):
                title, body = ARTICLES[template]
                # вузы, упомянутые в тексте, — их документы о приёме как источники (проверятся при публикации)
                src = [{"url": u.source_url, "title": f"{u.university}: документ о приёме {u.admission_year}"}
                       for u in universities if u.source_url and re.search(rf"\b{re.escape(u.university)}\b", body)]
                item = {"id": 0, "title": title, "body": body, "category": "guides", "sources_list": src,
                        "status": "draft"}
        return edit_page(request, item)

    @app.post("/admin/news/save", response_class=HTMLResponse)
    def admin_news_save(request: Request, csrf: str = Form(""), news_id: int = Form(0), title: str = Form(""),
                        body: str = Form(""), category: str = Form(""), sources: str = Form(""),
                        action: str = Form("draft")):
        """Сохранить, проверить ссылки или опубликовать. Публикация — только с рабочими ссылками."""
        import json as _json

        admin_post(request, csrf)
        title, body = " ".join(title.split())[:300], body.strip().replace("\r\n", "\n")[:20000]
        category = category if category in news_mod.CATEGORIES else "guides"
        src = news_mod.parse_sources(sources)
        errors = []
        if not title or not body:
            errors.append("Нужны заголовок и текст.")
        unofficial = [x["url"] for x in src if not news_mod.is_official(x["url"])]
        if unofficial:
            errors.append("Не официальные источники (агрегаторы и соцсети): " + ", ".join(unofficial))
        if action in ("check", "publish", "publish_drop"):
            src = check_all(src)
            broken = [x for x in src if not x["ok"]]
            if action == "publish_drop":
                src = [x for x in src if x["ok"]]
            elif broken and action == "publish":
                errors.append("Не открываются: " + ", ".join(x["url"] for x in broken)
                              + " — исправь ссылки или нажми «Убрать нерабочие и опубликовать».")
        draft = {"id": news_id, "title": title, "body": body, "category": category, "sources_list": src,
                 "status": "draft"}
        if errors or action == "check":
            return edit_page(request, draft, errors=errors, checked=action == "check" or bool(src))
        status = "published" if action in ("publish", "publish_drop") else "draft"
        payload = _json.dumps(src, ensure_ascii=False)
        with db() as s:
            if news_id:
                s.update_news(news_id, title=title, body=body, status=status, category=category, sources=payload)
            else:
                news_id = s.add_news(kind="article", title=title, body=body, status=status, category=category,
                                     sources=payload, source_url=src[0]["url"] if src else None)
        return RedirectResponse(f"/admin/news?status={status}", status_code=303)

    @app.get("/admin/news/generate", response_class=HTMLResponse)
    def admin_news_generate_form(request: Request):
        require_admin(request)
        from app.bot.texts import sort_key
        from app.news_seed import ARTICLES

        olympiads = sorted(catalog.olympiads.values(), key=lambda o: sort_key(short(o)))
        profiles = sorted({p for o in olympiads for p in o.profiles_in(catalog.current_year)})
        uni_programs = {u.university: [(p, program_label(p, names)) for p in university_programs(u)]
                        for u in universities}
        start = today()
        return render(request, "admin_news_generate.html", olympiads=olympiads, profiles=profiles,
                      uni_programs=uni_programs, templates=[t for t, _ in ARTICLES], start=start,
                      end=start + timedelta(days=30), categories=news_mod.CATEGORIES, cta=news_mod.CTA,
                      level_of=lambda o: o.best_level(catalog.current_year), error=request.query_params.get("error"))

    @app.post("/admin/news/generate")
    async def admin_news_generate(request: Request):
        """Собрать черновики по настройкам из формы: этапы, льготы вуза, подборка."""
        import json as _json

        from starlette.concurrency import run_in_threadpool

        form = await request.form()
        admin_post(request, str(form.get("csrf", "")))
        mode = str(form.get("mode", "stages"))
        ids = [catalog.canonical_id(str(v)) for v in form.getlist("olympiads") if catalog.get(str(v))]
        profiles = [str(v) for v in form.getlist("profiles") if str(v).strip()]
        unis = {str(v) for v in form.getlist("universities") if benefits.get(str(v))}
        diploma = "winner" if form.get("diploma") == "winner" else "prize"
        try:
            grade = min(11, max(9, int(str(form.get("grade", "11")))))
            start = date.fromisoformat(str(form.get("start") or today().isoformat()))
            end = date.fromisoformat(str(form.get("end") or (today() + timedelta(days=30)).isoformat()))
            level = int(str(form.get("level") or 0))
        except ValueError:
            return RedirectResponse("/admin/news/generate?error=Проверь+даты+и+класс", status_code=303)
        opts = news_mod.GenOptions(benefits=bool(form.get("with_benefits")), diploma=diploma, grade=grade,
                                   universities=unis or None, cta=tuple(str(v) for v in form.getlist("cta")))
        kinds = {str(v) for v in form.getlist("kinds")} or {"registration", "qualifying", "final"}
        with db() as s:
            stages = s.stages()
        if mode == "benefits":
            d = news_mod.benefits_news(catalog, benefits, str(form.get("university", "")),
                                       str(form.get("program", "")), diploma, ids, opts)
            drafts = [d] if d else []
        elif mode == "digest":
            drafts = news_mod.digest_news(catalog, benefits, stages, start, end, level, profiles, opts)
        else:
            if not ids:
                return RedirectResponse("/admin/news/generate?error=Выбери+олимпиады", status_code=303)
            drafts = news_mod.stage_news(catalog, benefits, stages, ids, profiles, kinds, start, end, opts,
                                         combined=form.get("combined") == "1")
        if not drafts:
            return RedirectResponse("/admin/news/generate?error=По+этим+настройкам+нечего+писать:+нет+этапов+"
                                    "или+льгот+в+периоде", status_code=303)
        created = []
        for d in drafts:
            if d.kind == "benefits":
                src = [{"url": d.source_url, "title": "Документ вуза об особых правах"}] if d.source_url else []
            else:
                o = catalog.get(d.olympiad_id) if d.olympiad_id else None
                src = news_mod.official_sources(o)
            src = await run_in_threadpool(check_all, src)
            src = [x for x in src if x["ok"]]
            category = {"benefits": "benefits", "digest": "digest"}.get(d.kind, "stages")
            with db() as s:
                created.append(s.add_news(kind=d.kind, title=d.title, body=d.body, olympiad_id=d.olympiad_id,
                                          profile=d.profile, event_date=d.event_date, category=category,
                                          sources=_json.dumps(src, ensure_ascii=False),
                                          source_url=src[0]["url"] if src else None))
        if len(created) == 1:
            return RedirectResponse(f"/admin/news/{created[0]}/edit", status_code=303)
        return RedirectResponse("/admin/news?status=draft", status_code=303)

    # -- «Предложить вуз» ------------------------------------------------------

    @app.post("/universities/suggest")
    def suggest_university(request: Request, csrf: str = Form(""), university: str = Form(""),
                           program: str = Form(""), contact: str = Form(""), website: str = Form("")):
        user = check_csrf(request, csrf) if current_user(request) else None
        university = " ".join(university.split())[:200]
        if website or not university:     # ловушка для ботов или пустая форма
            return RedirectResponse("/universities?suggested=1#suggest", status_code=303)
        with db() as s:
            if s.uni_requests_since(60) < 50 and not s.recent_uni_request(user, university):
                s.add_uni_request(university=university, program=" ".join(program.split())[:300],
                                  contact=contact.strip()[:200], telegram_id=user)
                if admin_id:
                    from html import escape as h

                    s.queue_message(admin_id, f"<b>Предложили вуз</b>\n{h(university)}"
                                              + (f"\nПрограмма: {h(program.strip()[:300])}" if program.strip() else "")
                                              + (f"\nКонтакт: {h(contact.strip()[:200])}" if contact.strip() else ""))
        return RedirectResponse("/universities?suggested=1#suggest", status_code=303)

    @app.get("/admin/requests", response_class=HTMLResponse)
    def admin_requests(request: Request):
        require_admin(request)
        with db() as s:
            rows = s.uni_requests("new")
        grouped: dict[str, list[dict]] = {}
        for r in rows:
            grouped.setdefault(r["university"].strip().lower(), []).append(r)
        groups = sorted(grouped.values(), key=lambda g: (-len(g), g[0]["university"]))
        return render(request, "admin_requests.html", groups=groups)

    @app.post("/admin/requests/done")
    def admin_requests_done(request: Request, csrf: str = Form(""), university: str = Form("")):
        admin_post(request, csrf)
        with db() as s:
            s.close_uni_requests(university)
        return RedirectResponse("/admin/requests", status_code=303)

    # -- статистика -------------------------------------------------------------

    @app.get("/admin/stats", response_class=HTMLResponse)
    def admin_stats(request: Request, days: int = Query(30, ge=1, le=365)):
        require_admin(request)
        with db() as s:
            data = stats_mod.report(s, catalog, today(), days, admin_id)
        return render(request, "admin_stats.html", r=data)

    @app.get("/admin/users", response_class=HTMLResponse)
    def admin_users(request: Request, q: str = "", only: str = ""):
        require_admin(request)
        with db() as s:
            rows = stats_mod.users(s, q, only)
        return render(request, "admin_users.html", rows=rows[:500], total=len(rows), q=q, only=only)

    @app.get("/admin/users.csv")
    def admin_users_csv(request: Request, q: str = "", only: str = ""):
        require_admin(request)
        with db() as s:
            rows = stats_mod.users(s, q, only)
        return Response(stats_mod.users_csv(rows), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="users.csv"'})

    # -- JSON ------------------------------------------------------------------

    @app.get("/api/olympiads")
    def api_olympiads(q: str = ""):
        found = [h.olympiad for h in catalog.search(q, limit=30)] if q else list(catalog.olympiads.values())
        return [{"id": o.id, "name": o.name, "is_vsosh": o.is_vsosh,
                 "profiles": sorted(o.profiles_in(catalog.current_year)),
                 "universities": len(benefits.for_olympiad(o.id))} for o in found]

    @app.get("/api/fit")
    def api_fit(olympiad: str, profile: str, diploma: str, grade: int = 0, year: str = ""):
        o = get_olympiad(olympiad)
        if diploma not in DIPLOMA_CHOICES.values():
            raise HTTPException(422, "diploma: winner или prize")
        _, _, found = fit_for(o, profile, diploma, grade or None, year if year in diploma_years
                              else catalog.current_year)
        return [{
            "university": u.university, "admission_year": u.admission_year, "status": u.document_status,
            "source_url": u.source_url,
            "benefits": [{"benefit": r.benefit, "diploma": r.diploma, "profiles": r.profiles, "levels": r.levels,
                          "grades": r.grades, "confirm_min_score": r.confirm_min_score,
                          "confirm_subject": r.confirm_subject, "score_for": r.score_for,
                          "programs": r.programs, "note": r.note} for r in rules],
        } for u, rules in found]

    @app.exception_handler(HTTPException)
    def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        return render(request, "404.html", status_code=exc.status_code, detail=exc.detail or "Такой страницы нет")

    @app.exception_handler(404)
    def not_found(request: Request, exc):
        return render(request, "404.html", status_code=404, detail=getattr(exc, "detail", "") or "Такой страницы нет")

    return app


__all__ = ["create_app", "slug", "summarize", "UniversityBenefits"]
