"""Сайт Бвишечки: олимпиады, вузы, льготы и подбор по диплому — те же данные, что у бота.

Запуск: py -m app.cli web run (http://127.0.0.1:8000). Страницы рендерятся на сервере (Jinja2),
у каждой льготы — ссылка на документ вуза и дата проверки. JSON для будущих клиентов — /api/…
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.benefits import (
    BENEFIT_TYPES, DIPLOMA_CHOICES, DIPLOMAS, BenefitsCatalog, Rule, fitting, olympiad_profiles,
    profile_level,
)
from app.catalog import Catalog, CatalogOlympiad

HERE = Path(__file__).parent
BOT_URL = "https://t.me/bvishechka_bot"
LEVELS = (1, 2, 3)
ROMAN = {1: "I", 2: "II", 3: "III"}


def slug(text: str) -> str:
    from app.export_pdf import _translit

    return re.sub(r"[^a-z0-9]+", "-", _translit(text.lower())).strip("-") or "vuz"


@dataclass
class BenefitGroup:
    """Сводка одной льготы вуза для олимпиады: «БВИ — победителям и призёрам: математика (12 программ)»."""
    benefit: str
    diploma: str
    profiles: list[tuple[str, int]]   # (профиль, число программ; 0 — «по профилю олимпиады»)
    scores: list[int]


def summarize(rules: list[Rule]) -> list[BenefitGroup]:
    groups: dict[tuple[str, str], dict[str, set[str] | None]] = {}
    scores: dict[tuple[str, str], set[int]] = {}
    for r in rules:
        key = (r.benefit, r.diploma)
        by_profile = groups.setdefault(key, {})
        for p in r.profiles or ["любой профиль"]:
            if r.programs is None:
                by_profile[p] = by_profile.get(p) or set()
            else:
                by_profile.setdefault(p, set()).update(r.programs)
        if r.confirm_min_score:
            scores.setdefault(key, set()).add(r.confirm_min_score)
    order = sorted(groups, key=lambda k: (list(BENEFIT_TYPES).index(k[0]), k[1] != "any"))
    return [BenefitGroup(b, d, sorted((p, len(progs or ())) for p, progs in groups[(b, d)].items()),
                         sorted(scores.get((b, d), ()))) for b, d in order]


def levels_label(levels: list[int] | None) -> str:
    if not levels or set(levels) == {1, 2, 3}:
        return ""
    lv = sorted(levels)
    return ROMAN[lv[0]] + (f"–{ROMAN[lv[-1]]}" if len(lv) > 1 else "") + " уровень"


def create_app(catalog: Catalog, benefits: BenefitsCatalog, *, stages_db: Path | None = None,
               today: Callable[[], date] = date.today) -> FastAPI:
    app = FastAPI(title="Бвишечка", docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/assets", StaticFiles(directory=HERE.parent / "assets"), name="assets")
    templates = Jinja2Templates(directory=HERE / "templates")
    admission_year = int(catalog.current_year[:4]) + 1
    universities = benefits.latest()
    by_slug = {slug(u.university): u for u in universities}
    uni_slug = {u.university: s for s, u in by_slug.items()}

    def upcoming(o: CatalogOlympiad):
        if not stages_db or not Path(stages_db).exists():
            return []
        from app.bot.storage import Storage

        storage = Storage(stages_db)
        try:
            return storage.upcoming_stages(o.id, today(), limit=6)
        finally:
            storage.close()

    def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
        base = dict(
            request=request, bot_url=BOT_URL, admission_year=admission_year, current_year=catalog.current_year,
            benefit_types=BENEFIT_TYPES, diplomas=DIPLOMAS, uni_slug=uni_slug, levels_label=levels_label,
            olympiad_count=sum(1 for o in catalog.olympiads.values() if o.profiles_in(catalog.current_year)),
            university_count=len(universities),
        )
        return templates.TemplateResponse(request, template, {**base, **context}, status_code=status_code)

    def get_olympiad(olympiad_id: str) -> CatalogOlympiad:
        o = catalog.get(olympiad_id)
        if o is None:
            raise HTTPException(404, "Олимпиада не найдена")
        return o

    def short(o: CatalogOlympiad) -> str:
        from app.bot.texts import button_name

        return "ВсОШ" if o.is_vsosh else button_name(o, 70)

    # -- страницы --------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        vsosh = next((o for o in catalog.olympiads.values() if o.is_vsosh), None)
        popular = [h.olympiad for q in ("Высшая проба", "Ломоносов", "Физтех", "Московская олимпиада школьников",
                                        "СПбГУ", "НТО") for h in catalog.search(q, limit=1)]
        return render(request, "index.html", vsosh=vsosh, popular=list({o.id: o for o in popular}.values()),
                      universities=universities, short=short)

    @app.get("/olympiads", response_class=HTMLResponse)
    def olympiads(request: Request, q: str = "", level: int = Query(0, ge=0, le=3)):
        year = catalog.current_year
        if q.strip():
            hits = catalog.search(q.strip()[:100], limit=60)
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
        return render(request, "olympiads.html", q=q, level=level, rows=rows, short=short)

    @app.get("/olympiad/{olympiad_id}", response_class=HTMLResponse)
    def olympiad(request: Request, olympiad_id: str, profile: str = "", diploma: str = "", grade: int = 0):
        o = get_olympiad(olympiad_id)
        if o.id != olympiad_id:  # старый id переименованной олимпиады
            return RedirectResponse(f"/olympiad/{o.id}", status_code=301)
        from app.bot.texts import PRIZE_BVI_WARNING, not_in_list_banner, prize_bvi_affected

        entries = benefits.for_olympiad(o.id)
        all_rules = [r for _, rs in entries for r in rs]
        profiles = olympiad_profiles(benefits, o.id)
        fit = None
        if profile in profiles and diploma in DIPLOMA_CHOICES.values():
            level, level_year = profile_level(o, profile, catalog.current_year)
            fit = dict(profile=profile, diploma=diploma, grade=grade or None, level=level, level_year=level_year,
                       entries=[(u, summarize(rs), rs) for u, rs in
                                fitting(benefits, o.id, profile, diploma, grade or None, level)])
        year_rows = []
        for name in sorted(o.profiles):
            by_year = o.profiles[name]
            year_rows.append((name, [by_year[y].level if y in by_year else None for y in catalog.years]))
        notes = [re.sub(r"<[^>]+>|[\U0001F000-\U0001FFFF]", "", n).strip()
                 for n in not_in_list_banner(o, all_rules, admission_year) if n]
        return render(
            request, "olympiad.html", o=o, name=short(o), entries=[(u, summarize(rs)) for u, rs in entries],
            profiles=profiles, fit=fit, form=dict(profile=profile, diploma=diploma, grade=grade),
            stages=upcoming(o), years=catalog.years, year_rows=year_rows,
            prize_warning=re.sub(r"<[^>]+>|[\U0001F000-\U0001FFFF☀-➿️]", "", PRIZE_BVI_WARNING).strip()
            if prize_bvi_affected(all_rules) else "",
            notes=notes,
        )

    @app.get("/olympiad/{olympiad_id}/pdf")
    def olympiad_pdf(olympiad_id: str, profile: str, diploma: str = "", grade: int = 0):
        from app.bot.texts import PRIZE_BVI_WARNING, not_in_list_banner, prize_bvi_affected
        from app.export_pdf import benefits_pdf, pdf_filename

        o = get_olympiad(olympiad_id)
        if profile not in olympiad_profiles(benefits, o.id):
            raise HTTPException(404, "У олимпиады нет льгот по этому профилю")
        if diploma in DIPLOMA_CHOICES.values():
            level, _ = profile_level(o, profile, catalog.current_year)
            entries = fitting(benefits, o.id, profile, diploma, grade or None, level)
            subtitle = f"Профиль «{profile}» · {'победитель' if diploma == 'winner' else 'призёр'}" + (
                f" · {grade} класс" if grade else "")
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
            counts = {k: sum(r.benefit == k for r in u.rules) for k in BENEFIT_TYPES}
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
        return render(request, "university.html", u=u, items=items, short=short)

    # -- JSON ------------------------------------------------------------------

    @app.get("/api/olympiads")
    def api_olympiads(q: str = ""):
        hits = catalog.search(q, limit=30) if q else [
            type("H", (), {"olympiad": o, "matched_profiles": []}) for o in catalog.olympiads.values()]
        return [{"id": h.olympiad.id, "name": h.olympiad.name, "is_vsosh": h.olympiad.is_vsosh,
                 "profiles": sorted(h.olympiad.profiles_in(catalog.current_year)),
                 "universities": len(benefits.for_olympiad(h.olympiad.id))} for h in hits]

    @app.get("/api/fit")
    def api_fit(olympiad: str, profile: str, diploma: str, grade: int = 0):
        o = get_olympiad(olympiad)
        if diploma not in DIPLOMA_CHOICES.values():
            raise HTTPException(422, "diploma: winner или prize")
        level, _ = profile_level(o, profile, catalog.current_year)
        return [{
            "university": u.university, "admission_year": u.admission_year, "status": u.document_status,
            "source_url": u.source_url,
            "benefits": [{"benefit": r.benefit, "diploma": r.diploma, "profiles": r.profiles, "levels": r.levels,
                          "grades": r.grades, "confirm_min_score": r.confirm_min_score,
                          "confirm_subject": r.confirm_subject, "score_for": r.score_for,
                          "programs": r.programs, "note": r.note} for r in rules],
        } for u, rules in fitting(benefits, o.id, profile, diploma, grade or None, level)]

    @app.exception_handler(404)
    def not_found(request: Request, exc):
        detail = getattr(exc, "detail", "") or "Такой страницы нет"
        return render(request, "404.html", status_code=404, detail=detail)

    return app

