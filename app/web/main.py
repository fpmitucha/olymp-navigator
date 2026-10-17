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
from datetime import date
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
               today: Callable[[], date] = date.today, secret: str = "") -> FastAPI:
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
        if user:
            with db() as s:
                name = s.profile(user)["name"]
        base = dict(
            request=request, bot_url=BOT_URL, admission_year=admission_year, current_year=catalog.current_year,
            benefit_types=BENEFIT_TYPES, diplomas=DIPLOMAS, uni_slug=uni_slug, levels_label=levels_label,
            olympiad_count=olympiad_count, university_count=len(universities), user=user, user_name=name,
            csrf=csrf_token(request) if user else "", short=short, diploma_years=diploma_years,
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
        return render(request, "index.html", popular=list({o.id: o for o in popular}.values()),
                      routes=routes, uni_counts=uni_counts)

    @app.get("/olympiads", response_class=HTMLResponse)
    def olympiads(request: Request, q: str = "", level: int = Query(0, ge=0, le=3)):
        year = catalog.current_year
        q = " ".join(q.split())[:100]
        if q:
            hits = catalog.search(q, limit=60)
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
        return render(request, "universities.html", rows=rows)

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
        response = RedirectResponse("/me", status_code=303)
        response.set_cookie(SESSION_COOKIE, sid, max_age=60 * 60 * 24 * 60, httponly=True, samesite="lax",
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
                      today=today(), best_by_uni=best_by_uni, all_universities=universities)

    @app.post("/me/profile")
    def me_profile(request: Request, csrf: str = Form(""), name: str = Form(""), grade: int = Form(0)):
        user = check_csrf(request, csrf)
        with db() as s:
            s.save_profile(user, " ".join(name.split())[:60], grade if 5 <= grade <= 11 else None)
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
