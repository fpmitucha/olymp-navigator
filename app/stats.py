"""Статистика для админки: посещаемость сайта, бот, поиск, входы, удержание, источники и воронка.

Данные собираются сами: просмотры страниц сайта (с обезличенным id браузера), действия в боте по дням,
поисковые запросы, входы на сайт. Действия администратора в статистику не попадают.
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from datetime import date, timedelta

from app.bot.storage import Storage
from app.catalog import Catalog


def _week(d: str) -> str:
    """Понедельник недели (ISO-дата) — ключ когорты."""
    x = date.fromisoformat(d[:10])
    return (x - timedelta(days=x.weekday())).isoformat()


def _pct(a: int, b: int) -> int:
    return round(100 * a / b) if b else 0


def retention(first: dict, active_days: dict, today: date, weeks: int = 6, cohorts: int = 8) -> list[dict]:
    """Когорты по неделе первого появления: какая доля вернулась через 1, 2, … недель."""
    by_cohort: dict[str, list] = defaultdict(list)
    for who, first_day in first.items():
        by_cohort[_week(first_day)].append(who)
    rows = []
    this_week = _week(today.isoformat())
    for cohort in sorted(by_cohort, reverse=True)[:cohorts]:
        members = by_cohort[cohort]
        start = date.fromisoformat(cohort)
        cells = []
        for w in range(1, weeks + 1):
            ws = start + timedelta(weeks=w)
            if ws.isoformat() > this_week:
                cells.append(None)       # неделя ещё не наступила
                continue
            we = ws + timedelta(days=6)
            back = sum(1 for m in members
                       if any(ws.isoformat() <= d <= we.isoformat() for d in active_days.get(m, ())))
            cells.append(_pct(back, len(members)))
        rows.append({"cohort": cohort, "size": len(members), "cells": cells})
    return rows


def report(s: Storage, catalog: Catalog, today: date, days: int, admin_id: int = 0) -> dict:
    since = (today - timedelta(days=days - 1)).isoformat()
    me = admin_id or -1
    pv_where = "day >= ? AND coalesce(telegram_id, 0) != ?"
    q = s.query

    summary = q(f"SELECT count(*) AS views, count(DISTINCT visitor) AS visitors, "
                f"count(DISTINCT visitor || day) AS visits FROM pageview WHERE {pv_where}", (since, me))[0]
    summary["new_visitors"] = q("SELECT count(*) AS n FROM visitor WHERE first_ts >= ? AND coalesce(telegram_id, 0) != ?",
                                (since, me))[0]["n"]
    summary["logins"] = q("SELECT count(*) AS n FROM auth_log WHERE ts >= ? AND telegram_id != ?", (since, me))[0]["n"]
    summary["login_users"] = q("SELECT count(DISTINCT telegram_id) AS n FROM auth_log WHERE ts >= ? AND telegram_id != ?",
                               (since, me))[0]["n"]
    summary["bot_active"] = q("SELECT count(DISTINCT telegram_id) AS n FROM bot_activity WHERE day >= ? "
                              "AND telegram_id != ?", (since, me))[0]["n"]
    summary["bot_new"] = q("SELECT count(*) AS n FROM bot_user WHERE created_at >= ? AND telegram_id != ?",
                           (since, me))[0]["n"]
    summary["bot_total"] = q("SELECT count(*) AS n FROM bot_user WHERE telegram_id != ?", (me,))[0]["n"]
    summary["bot_blocked"] = q("SELECT count(*) AS n FROM bot_user WHERE is_active = 0")[0]["n"]
    summary["phones"] = q("SELECT count(*) AS n FROM bot_user WHERE phone IS NOT NULL")[0]["n"]
    summary["help"] = q("SELECT count(*) AS n FROM help_request WHERE created_at >= ?", (since,))[0]["n"]
    summary["crm"] = q("SELECT count(*) AS n FROM crm_client")[0]["n"]
    summary["subs"] = q("SELECT count(*) AS n FROM subscription")[0]["n"]
    summary["pages_per_visit"] = round(summary["views"] / summary["visits"], 1) if summary["visits"] else 0

    # по дням
    web_days = {r["day"]: r for r in q(f"SELECT day, count(DISTINCT visitor) AS visitors, count(*) AS views "
                                       f"FROM pageview WHERE {pv_where} GROUP BY day", (since, me))}
    bot_days = {r["day"]: r["n"] for r in q("SELECT day, count(DISTINCT telegram_id) AS n FROM bot_activity "
                                            "WHERE day >= ? AND telegram_id != ? GROUP BY day", (since, me))}
    daily = []
    for i in range(days):
        d = (today - timedelta(days=days - 1 - i)).isoformat()
        daily.append({"day": d, "visitors": web_days.get(d, {}).get("visitors", 0),
                      "views": web_days.get(d, {}).get("views", 0), "bot": bot_days.get(d, 0)})
    peak = max([1] + [max(x["visitors"], x["bot"]) for x in daily])

    def top_paths(prefix: str, limit: int = 15) -> list[dict]:
        return q(f"SELECT path, count(*) AS views, count(DISTINCT visitor) AS visitors FROM pageview "
                 f"WHERE {pv_where} AND path LIKE ? GROUP BY path ORDER BY views DESC LIMIT ?",
                 (since, me, prefix + "%", limit))

    pages = top_paths("/")
    olympiads = []
    for r in top_paths("/olympiad/", 40):
        parts = r["path"].split("/")
        o = catalog.get(parts[2]) if len(parts) > 2 else None
        if o:
            olympiads.append(dict(r, name=o.name))
    by_olympiad: dict[str, dict] = {}
    for r in olympiads:
        cur = by_olympiad.setdefault(r["name"], {"name": r["name"], "views": 0, "visitors": 0})
        cur["views"] += r["views"]
        cur["visitors"] += r["visitors"]
    olympiads = sorted(by_olympiad.values(), key=lambda x: -x["views"])[:15]

    searches = q("SELECT lower(query) AS query, count(*) AS n, round(avg(results), 1) AS results, "
                 "sum(source = 'bot') AS bot, sum(source = 'site') AS site FROM search_log WHERE ts >= ? "
                 "AND coalesce(telegram_id, 0) != ? GROUP BY lower(query) ORDER BY n DESC LIMIT 30", (since, me))
    empty = q("SELECT lower(query) AS query, count(*) AS n FROM search_log WHERE ts >= ? AND results = 0 "
              "AND coalesce(telegram_id, 0) != ? GROUP BY lower(query) ORDER BY n DESC LIMIT 20", (since, me))

    sources = q("SELECT CASE WHEN utm_source != '' THEN 'utm: ' || utm_source WHEN ref_host != '' THEN ref_host "
                "ELSE 'прямой заход' END AS source, count(*) AS visitors, count(telegram_id) AS logged "
                "FROM visitor WHERE first_ts >= ? GROUP BY source ORDER BY visitors DESC LIMIT 20", (since,))
    campaigns = q("SELECT v.utm_campaign AS campaign, v.utm_source AS source, count(*) AS visitors, "
                  "count(v.telegram_id) AS logged, count(DISTINCT h.telegram_id) AS help "
                  "FROM visitor v LEFT JOIN help_request h ON h.telegram_id = v.telegram_id "
                  "WHERE v.first_ts >= ? AND v.utm_campaign != '' GROUP BY v.utm_campaign, v.utm_source "
                  "ORDER BY visitors DESC", (since,))
    devices = q(f"SELECT device, count(DISTINCT visitor) AS visitors FROM pageview WHERE {pv_where} "
                f"GROUP BY device ORDER BY visitors DESC", (since, me))

    # воронка
    interested = q(f"SELECT count(DISTINCT visitor) AS n FROM pageview WHERE {pv_where} AND "
                   f"(path LIKE '/olympiad/%' OR path LIKE '/route%' OR path LIKE '/university/%')", (since, me))[0]["n"]
    logged_visitors = q("SELECT count(DISTINCT p.visitor) AS n FROM pageview p JOIN visitor v ON v.visitor = p.visitor "
                        "WHERE p.day >= ? AND v.telegram_id IS NOT NULL AND v.telegram_id != ?", (since, me))[0]["n"]
    funnel = [("Зашли на сайт", summary["visitors"]), ("Смотрели олимпиады, вузы или подбор", interested),
              ("Вошли через бота", logged_visitors), ("Оставили заявку на сопровождение", summary["help"]),
              ("Клиенты сопровождения (всего)", summary["crm"])]
    top = funnel[0][1] or 1
    funnel = [{"step": a, "n": b, "pct": _pct(b, top)} for a, b in funnel]

    # удержание: бот (по telegram_id) и сайт (по браузеру)
    first_bot = {r["telegram_id"]: r["created_at"][:10] for r in q("SELECT telegram_id, created_at FROM bot_user "
                                                                   "WHERE telegram_id != ?", (me,))}
    act_bot: dict = defaultdict(set)
    for r in q("SELECT telegram_id, day FROM bot_activity"):
        act_bot[r["telegram_id"]].add(r["day"])
    for r in q("SELECT DISTINCT telegram_id, day FROM pageview WHERE telegram_id IS NOT NULL"):
        act_bot[r["telegram_id"]].add(r["day"])
    first_web = {r["visitor"]: r["first_ts"][:10] for r in q("SELECT visitor, first_ts FROM visitor "
                                                            "WHERE coalesce(telegram_id, 0) != ?", (me,))}
    act_web: dict = defaultdict(set)
    for r in q("SELECT DISTINCT visitor, day FROM pageview"):
        act_web[r["visitor"]].add(r["day"])

    return {
        "days": days, "since": since, "summary": summary, "daily": daily, "peak": peak, "pages": pages,
        "olympiads": olympiads, "universities": top_paths("/university/"), "news": top_paths("/news/"),
        "searches": searches, "empty": empty, "sources": sources, "campaigns": campaigns, "devices": devices,
        "funnel": funnel, "ret_bot": retention(first_bot, act_bot, today), "ret_web": retention(first_web, act_web, today),
    }


USER_SQL = """
SELECT u.telegram_id, u.username, u.first_name, u.last_name, u.phone, u.created_at, u.last_seen, u.is_active,
       p.name AS profile_name, p.grade, uc.code,
       (SELECT count(*) FROM bot_activity a WHERE a.telegram_id = u.telegram_id) AS bot_days,
       (SELECT count(DISTINCT day) FROM pageview v WHERE v.telegram_id = u.telegram_id) AS web_days,
       (SELECT max(ts) FROM pageview v WHERE v.telegram_id = u.telegram_id) AS web_last,
       (SELECT count(*) FROM auth_log l WHERE l.telegram_id = u.telegram_id) AS logins,
       (SELECT count(*) FROM subscription s WHERE s.telegram_id = u.telegram_id) AS subs,
       (SELECT count(*) FROM user_diploma d WHERE d.telegram_id = u.telegram_id) AS diplomas,
       (SELECT count(*) FROM help_request h WHERE h.telegram_id = u.telegram_id) AS help,
       EXISTS (SELECT 1 FROM crm_client c WHERE c.telegram_id = u.telegram_id) AS crm,
       (SELECT CASE WHEN utm_source != '' THEN utm_source || ' / ' || utm_campaign WHEN ref_host != '' THEN ref_host
               ELSE '' END FROM visitor x WHERE x.telegram_id = u.telegram_id ORDER BY first_ts LIMIT 1) AS source
FROM bot_user u LEFT JOIN user_profile p ON p.telegram_id = u.telegram_id
LEFT JOIN user_code uc ON uc.telegram_id = u.telegram_id
"""


def users(s: Storage, q: str = "", only: str = "") -> list[dict]:
    rows = s.query(USER_SQL)
    for r in rows:
        r["last"] = max(filter(None, [r["last_seen"], r["web_last"], r["created_at"]]))
        r["display"] = " ".join(filter(None, [r["first_name"], r["last_name"]])) or r["profile_name"] or ""
    q = q.strip().lower().lstrip("@")
    if q:
        rows = [r for r in rows if q in " ".join(str(r[k] or "") for k in
                                                 ("username", "display", "profile_name", "phone", "code",
                                                  "telegram_id")).lower()]
    if only == "phone":
        rows = [r for r in rows if r["phone"]]
    elif only == "site":
        rows = [r for r in rows if r["logins"]]
    elif only == "crm":
        rows = [r for r in rows if r["crm"]]
    return sorted(rows, key=lambda r: r["last"], reverse=True)


def users_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    cols = ["telegram_id", "username", "display", "phone", "grade", "code", "created_at", "last", "bot_days",
            "web_days", "logins", "subs", "diplomas", "help", "crm", "source"]
    w = csv.writer(out, delimiter=";")
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") if r.get(c) is not None else "" for c in cols])
    return "﻿" + out.getvalue()   # BOM — чтобы Excel открыл кириллицу


def device_of(user_agent: str) -> str:
    ua = user_agent.lower()
    return "mobile" if any(k in ua for k in ("iphone", "android", "mobile", "ipad")) else "desktop"


BOT_UA = ("bot", "spider", "crawl", "preview", "curl", "python", "monitor", "headless", "wget", "httpx")


def is_bot(user_agent: str) -> bool:
    ua = user_agent.lower()
    return not ua or any(k in ua for k in BOT_UA)


__all__ = ["report", "users", "users_csv", "device_of", "is_bot", "Counter"]
