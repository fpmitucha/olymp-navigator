"""Синхронизация этапов: поддельный olimpiada.ru из фрагментов настоящих страниц."""

from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path

from app.bot.storage import Storage
from app.catalog import Catalog
from app.parsers.olimpiada import parse_activity
from app.stages_sync import META_DISCOVERED, META_PARENTS, META_RELEVANT, match_olympiad, match_profile, sync_stages
from tests.helpers import olympiad, profile, rsr_list

FIXTURES = Path(__file__).parent / "fixtures" / "olimpiada"
TODAY = date(2026, 10, 15)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def catalog() -> Catalog:
    """Каталог с номерами перечня 2026/27, как у страниц-фрагментов."""
    return Catalog([rsr_list(
        "2026/27",
        olympiad("Олимпиада школьников «Физтех»", profile("математика", 1), profile("физика", 1),
                 profile("информатика и программирование", 2), number=38),
        olympiad("Всероссийская междисциплинарная олимпиада школьников 8–11 классов «Национальная технологическая "
                 "олимпиада»", profile("искусственный интеллект", 2), profile("летающая робототехника", 3), number=3),
        olympiad("Межрегиональные предметные олимпиады «Казанский (Приволжский) федеральный университет»",
                 profile("дизайн", 1), profile("химия", 1), number=23),
        olympiad("Отраслевая физико-математическая олимпиада школьников «Росатом»",
                 profile("математика", 2), profile("физика", 1), profile("информатика", 1), number=52),
        olympiad("Московская олимпиада школьников", profile("математика", 1), profile("физика", 1), number=26),
    )])


class FakeSite:
    """olimpiada.ru из фрагментов. 411 («Росатом» по математике) собран из страницы 5881:
    его нет в фрагментах, а нужен, чтобы через общую страницу 201 найти 5881, которой нет в sitemap."""

    def __init__(self):
        self.pages = {aid: fixture(f"activity_{aid}.html") for aid in (1, 201, 395, 5369, 5755, 5881)}
        self.pages[411] = (self.pages[5881].replace("по информатике", "по математике")
                           .replace("icon-informatics", "icon-math").replace("Информатика", "Математика"))
        self.sitemap = [395, 5369, 5755, 1, 411, 777]
        self.calls: list[str] = []

    def fetch(self, url: str) -> str:
        self.calls.append(url)
        if url.endswith("/sitemap.xml"):
            return "".join(f"<loc>https://olimpiada.ru/activity/{i}</loc>" for i in self.sitemap)
        activity_id = int(url.rsplit("/", 1)[1])
        if activity_id in self.pages:
            return self.pages[activity_id]
        if activity_id == 777:
            return "<h1>Олимпиада не из перечня</h1>"
        return f"<h1>Общая страница {activity_id}</h1>"  # общие страницы Физтеха, МОШ, НТО, КФУ


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.c = catalog()

    def activity(self, aid: int):
        return parse_activity(fixture(f"activity_{aid}.html"), aid, TODAY)

    def test_by_number_and_profile(self):
        cases = {395: ("Физтех", "математика"), 5755: ("Казанский", "дизайн"),
                 5881: ("Росатом", "информатика"), 1: ("Московская", "математика"), 5369: ("Национальная", None)}
        for aid, (name, expected_profile) in cases.items():
            a = self.activity(aid)
            o = match_olympiad(self.c, a)
            self.assertIn(name, o.name, aid)
            self.assertEqual(match_profile(o, a, "2026/27"), expected_profile, aid)

    def test_fallback_by_name(self):
        a = self.activity(395)
        a.rsr_number = 999  # номер не совпал — ищем по общей странице «Олимпиада школьников «Физтех»»
        self.assertIn("Физтех", match_olympiad(self.c, a).name)
        a.parent_name, a.name = None, "Совсем другая олимпиада"
        self.assertIsNone(match_olympiad(self.c, a))


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.c = catalog()
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)
        self.site = FakeSite()

    def sync(self, **kwargs):
        return sync_stages(self.c, self.storage, self.site.fetch, TODAY, **kwargs)

    def stages_by_activity(self) -> dict[int, list[str]]:
        rows = self.storage.conn.execute("SELECT external_id, title FROM stage ORDER BY id").fetchall()
        result: dict[int, list[str]] = {}
        for r in rows:
            result.setdefault(int(r["external_id"].split(":")[1]), []).append(r["title"])
        return result

    def test_discovery_finds_pages_missing_from_sitemap(self):
        report = self.sync()
        self.assertTrue(report.discovery)
        relevant = json.loads(self.storage.get_meta(META_RELEVANT))
        self.assertEqual(relevant, [1, 395, 411, 5369, 5755, 5881])  # 5881 — через общую страницу 201
        self.assertIn(201, json.loads(self.storage.get_meta(META_PARENTS)))
        self.assertIsNotNone(self.storage.get_meta(META_DISCOVERED))
        self.assertEqual(report.unmatched, [])
        self.assertEqual(report.errors, [])
        stages = self.stages_by_activity()
        self.assertEqual(stages[395], ["Регистрация на второй тур", "Второй тур",
                                       "Регистрация на третий тур для 11 классов", "Третий тур для 11 классов", "Финал"])
        self.assertNotIn(5881, stages)                  # расписания нет
        self.assertNotIn("Заключительный этап", stages[5369])  # «Уточняется» — без даты не сохраняем
        self.assertEqual(report.without_schedule, 2)    # 5881 и 411

    def test_stage_fields(self):
        self.sync()
        fiztech = self.c.by_number("2026/27", 38)
        final = next(s for s in self.storage.stages(fiztech.id) if s.title == "Финал")
        self.assertEqual((final.kind, final.starts_at, final.profile), ("final", date(2027, 2, 14), "математика"))
        self.assertEqual(final.source_url, "https://olimpiada.ru/activity/395/events/23152")
        reg = next(s for s in self.storage.stages(fiztech.id) if s.title == "Регистрация на второй тур")
        self.assertEqual((reg.kind, reg.starts_at, reg.ends_at), ("registration", None, date(2026, 10, 31)))

    def test_daily_run_skips_sitemap_and_updates(self):
        self.sync()
        self.site.calls.clear()
        # на сайте убрали финал Физтеха и перенесли второй тур
        self.site.pages[395] = (self.site.pages[395]
                                .replace('<tr class="notgreyclass" data="none"> <td colspan="2"><a href="/activity/395/events/23152">'
                                         '<div class="event_name">Финал</div></a></td> <td colspan="1">'
                                         '<a href="/activity/395/events/23152">14&nbsp;фев</a></td> </tr>', "")
                                .replace("1&nbsp;ноя", "2&nbsp;ноя"))
        report = self.sync()
        self.assertFalse(report.discovery)
        self.assertFalse(any(u.endswith("sitemap.xml") for u in self.site.calls))
        self.assertEqual(report.stages_removed, 1)
        fiztech = self.c.by_number("2026/27", 38)
        titles = {s.title: s for s in self.storage.stages(fiztech.id)}
        self.assertNotIn("Финал", titles)
        self.assertEqual(titles["Второй тур"].starts_at, date(2026, 11, 2))

    def test_page_leaving_list_removes_stages(self):
        self.sync()
        self.site.pages[5755] = self.site.pages[5755].replace("В 2026/27 году номер 23", "В 2025/26 году номер 23")
        report = self.sync()
        self.assertNotIn(5755, self.stages_by_activity())
        self.assertGreaterEqual(report.stages_removed, 3)
        self.assertNotIn(5755, json.loads(self.storage.get_meta(META_RELEVANT)))

    def test_past_events_not_saved(self):
        late = date(2026, 11, 2)
        sync_stages(self.c, self.storage, self.site.fetch, late)
        self.assertNotIn("Второй тур", self.stages_by_activity()[395])  # прошёл 1 ноября

    def test_limit_is_a_dry_run_for_saved_list(self):
        report = self.sync(limit=2)
        self.assertLessEqual(report.activities, 2)
        self.assertIsNone(self.storage.get_meta(META_RELEVANT))

    def test_errors_do_not_stop_sync(self):
        original = self.site.fetch

        def flaky(url):
            if url.endswith("/activity/5755"):
                raise RuntimeError("timeout")
            return original(url)

        report = sync_stages(self.c, self.storage, flaky, TODAY)
        self.assertEqual(len(report.errors), 1)
        self.assertIn(395, self.stages_by_activity())

    def test_temporary_error_keeps_known_page(self):
        self.sync()
        before = self.stages_by_activity()[5755]
        original = self.site.fetch
        self.site.fetch = lambda url: (_ for _ in ()).throw(RuntimeError("timeout")) \
            if url.endswith("/activity/5755") else original(url)
        report = self.sync()
        self.assertEqual(len(report.errors), 1)
        self.assertIn(5755, json.loads(self.storage.get_meta(META_RELEVANT)))
        self.assertEqual(self.stages_by_activity()[5755], before)  # этапы не удалены


    def test_removed_page_is_not_an_error(self):
        from app.fetch import PageGone

        self.sync()
        original = self.site.fetch
        self.site.fetch = lambda url: (_ for _ in ()).throw(PageGone("404")) \
            if url.endswith("/activity/5755") else original(url)
        report = self.sync()
        self.assertEqual(report.errors, [])
        self.assertNotIn(5755, json.loads(self.storage.get_meta(META_RELEVANT)))  # больше не следим
        self.assertNotIn(5755, self.stages_by_activity())  # этапы удалённой страницы убраны


if __name__ == "__main__":
    unittest.main()
