import unittest
from datetime import date, datetime, timedelta, timezone

from app.bot.storage import Storage
from app.stages import StageRow

try:
    from fastapi.testclient import TestClient

    HAS_WEB = True
except ImportError:  # pragma: no cover
    HAS_WEB = False


class NewsGenerateTest(unittest.TestCase):
    def setUp(self):
        from app.benefits import BenefitsCatalog, parse_file
        from tests.helpers import sample_catalog
        from tests.test_benefits import university

        self.catalog = sample_catalog()
        self.benefits = BenefitsCatalog([parse_file(university(), self.catalog, "t.json")])
        self.o = self.catalog.search("Физтех")[0].olympiad
        self.s = Storage(":memory:")

    def rows(self):
        y = self.catalog.current_year
        return [
            StageRow(olympiad_id=self.o.id, kind="registration", title="Регистрация", starts_at=date(2026, 11, 2),
                     ends_at=date(2026, 11, 20), profile="математика", academic_year=y, source_url="https://src",
                     external_id="olimpiada:1:1"),
            StageRow(olympiad_id=self.o.id, kind="qualifying", title="Отборочный этап", starts_at=date(2026, 12, 1),
                     ends_at=date(2026, 12, 2), profile="математика", academic_year=y, source_url=None,
                     external_id="olimpiada:1:2"),
        ]

    def test_drafts_and_dedupe(self):
        from app.bot.notifier import news_housekeeping

        self.s.set_meta("news_since", "2000-01-01T00:00:00+00:00")
        self.s.upsert_stages(self.rows())
        added = news_housekeeping(self.s, self.catalog, self.benefits, date(2026, 11, 4), check_links=False)
        drafts = self.s.news_list("draft")
        kinds = sorted(d["kind"] for d in drafts)
        self.assertEqual(added, 2)
        self.assertEqual(kinds, ["registration_open", "stage_dates"])
        self.assertTrue(any("Тестовый вуз" in d["body"] for d in drafts))     # льготы в тексте
        self.assertEqual(news_housekeeping(self.s, self.catalog, self.benefits, date(2026, 11, 4), check_links=False), 0)  # без повторов
        # за 3 дня до конца — «осталось 3 дня»; в понедельник — подборка
        news_housekeeping(self.s, self.catalog, self.benefits, date(2026, 11, 17), check_links=False)
        self.assertIn("registration_closing", {d["kind"] for d in self.s.news_list("draft")})
        news_housekeeping(self.s, self.catalog, self.benefits, date(2026, 11, 30), check_links=False)  # понедельник
        self.assertIn("digest", {d["kind"] for d in self.s.news_list("draft")})

    def test_first_sync_does_not_flood(self):
        from app.bot.notifier import news_housekeeping

        self.s.upsert_stages(self.rows())        # этапы загружены до запуска ленты
        future = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(timespec="seconds")
        self.s.set_meta("news_since", future)
        news_housekeeping(self.s, self.catalog, self.benefits, date(2026, 11, 4), check_links=False)
        self.assertEqual([d["kind"] for d in self.s.news_list("draft")], ["registration_open"])

    def test_channel_post(self):
        import asyncio

        import app.bot.notifier as n

        nid = self.s.add_news(kind="article", title="Привет", body="Текст <b>", status="published")
        sent = []

        async def send(chat, text):
            sent.append((chat, text))

        pause, n.SEND_PAUSE = n.SEND_PAUSE, 0
        try:
            self.assertEqual(asyncio.run(n.post_news(self.s, send, "", "")), 0)       # канал не задан
            asyncio.run(n.post_news(self.s, send, "@ch", "https://bvishechka.com"))
        finally:
            n.SEND_PAUSE = pause
        self.assertEqual(sent[0][0], "@ch")
        self.assertIn(f"https://bvishechka.com/news/{nid}-privet", sent[0][1])
        self.assertIn("&lt;b&gt;", sent[0][1])
        self.assertEqual(self.s.news_to_post(), [])


@unittest.skipUnless(HAS_WEB, "fastapi не установлен")
class NewsWebTest(unittest.TestCase):
    def setUp(self):
        import re

        from app.benefits import BenefitsCatalog, parse_file
        from app.web.main import create_app
        from tests.helpers import sample_catalog
        from tests.test_benefits import university

        catalog = sample_catalog()
        self.app = create_app(catalog, BenefitsCatalog([parse_file(university(), catalog, "t.json")]), admin_id=1000)
        self.anon = TestClient(self.app)
        self.admin = TestClient(self.app)
        token = re.search(r"token=([\w-]+)", self.admin.get("/login").text).group(1)
        with self.app.state.db() as s:
            s.confirm_login(token, 1000)
        finish = self.admin.get(f"/login/finish?token={token}", follow_redirects=False)
        self.admin.cookies.set("bv_sid", finish.cookies["bv_sid"])
        self.csrf = re.search(r'name="csrf" value="(\w+)"', self.admin.get("/me").text).group(1)

    def test_moderation_and_pages(self):
        with self.app.state.db() as s:
            nid = s.add_news(kind="registration_open", title="Открылась регистрация: Тест", body="До 20 ноября.",
                             dedupe_key="k1")
        self.assertEqual(self.anon.get(f"/news/{nid}").status_code, 404)          # черновик не виден
        self.assertIn("Открылась регистрация: Тест", self.admin.get("/admin/news").text)
        self.assertEqual(self.anon.get("/admin/news").status_code, 404)
        self.admin.post("/admin/news/bulk", data={"csrf": self.csrf, "action": "publish", "ids": [str(nid)]})
        page = self.anon.get(f"/news/{nid}", follow_redirects=False)
        self.assertEqual(page.status_code, 301)
        self.assertIn("До 20 ноября", self.anon.get(page.headers["location"]).text)
        self.assertIn("Открылась регистрация: Тест", self.anon.get("/news").text)
        self.assertIn("Открылась регистрация: Тест", self.anon.get("/").text)
        self.assertIn("<rss", self.anon.get("/news/rss.xml").text)
        self.admin.post("/admin/news/save", data={"csrf": self.csrf, "title": "Как подтвердить БВИ",
                                                  "body": "Абзац 1\n\nАбзац 2", "action": "publish"})
        self.assertIn("Как подтвердить БВИ", self.anon.get("/news").text)

    def test_suggest_university(self):
        r = self.anon.post("/universities/suggest", data={"university": "МИСИС", "program": "ПИ", "contact": "@me"},
                           follow_redirects=False)
        self.assertIn("suggested=1", r.headers["location"])
        self.anon.post("/universities/suggest", data={"university": "мисис"})       # повтор за сутки
        self.anon.post("/universities/suggest", data={"university": "Спам", "website": "x"})  # ловушка
        page = self.admin.get("/admin/requests").text
        self.assertIn("МИСИС", page)
        self.assertNotIn("Спам", page)
        with self.app.state.db() as s:
            self.assertTrue(any("МИСИС" in m["text"] for m in s.pending_messages()))   # пришло админу
        self.admin.post("/admin/requests/done", data={"csrf": self.csrf, "university": "МИСИС"})
        self.assertNotIn("МИСИС", self.admin.get("/admin/requests").text)
        self.assertIn("Нет нужного вуза", self.anon.get("/universities").text)


@unittest.skipUnless(HAS_WEB, "fastapi не установлен")
class StatsWebTest(NewsWebTest):
    def test_moderation_and_pages(self):  # проверено выше
        pass

    def test_suggest_university(self):
        pass

    def test_stats_and_users(self):
        browser = TestClient(self.app)
        ua = {"User-Agent": "Mozilla/5.0 (iPhone)"}
        with self.app.state.db() as s:
            before = s.query("SELECT count(*) AS n FROM pageview")[0]["n"]
        browser.get("/?utm_source=vk&utm_campaign=sh2", headers=ua)
        browser.get("/olympiads?q=Физтех", headers=ua, follow_redirects=False)
        browser.get("/olympiads?q=несуществующая", headers=ua)
        TestClient(self.app).get("/", headers={"User-Agent": "Googlebot"})     # боты не считаются
        self.admin.get("/", headers=ua)                                          # и куратор тоже
        with self.app.state.db() as s:
            s.log_bot_user(55, "masha", "Маша", None, "2026-11-04")
            s.set_phone(55, "+79000000000")
            views = s.query("SELECT count(*) AS n FROM pageview")[0]["n"]
        self.assertEqual(views - before, 2)      # главная и пустой поиск; редирект, бот и куратор — нет
        page = self.admin.get("/admin/stats?days=7").text
        self.assertIn("несуществующая", page)        # поиск без результатов
        self.assertIn("sh2", page)                   # utm-кампания
        self.assertIn("mobile", page)
        users = self.admin.get("/admin/users").text
        self.assertIn("@masha", users)
        self.assertIn("+79000000000", users)
        self.assertIn("masha", self.admin.get("/admin/users.csv").text)
        self.assertEqual(TestClient(self.app).get("/admin/stats").status_code, 404)


@unittest.skipUnless(HAS_WEB, "fastapi не установлен")
class GeneratorTest(NewsWebTest):
    def test_moderation_and_pages(self):
        pass

    def test_suggest_university(self):
        pass

    def setUp(self):
        super().setUp()
        import app.news as news_mod

        self._check = news_mod.check_url
        news_mod.check_url = lambda url, timeout=8: "broken" not in url     # без сети в тестах
        self.o = self.app.state.db  # noqa
        from tests.helpers import sample_catalog

        cat = sample_catalog()
        self.fiz = cat.search("Физтех")[0].olympiad
        y = cat.current_year
        with self.app.state.db() as s:
            s.upsert_stages([
                StageRow(olympiad_id=self.fiz.id, kind="registration", title="Регистрация", starts_at=date(2026, 11, 1),
                         ends_at=date(2026, 11, 20), profile=None, academic_year=y, source_url=None,
                         external_id="olimpiada:1:1"),
                StageRow(olympiad_id=self.fiz.id, kind="qualifying", title="Онлайн-этап", starts_at=date(2026, 11, 25),
                         ends_at=date(2026, 11, 26), profile="математика", academic_year=y, source_url=None,
                         external_id="olimpiada:1:2")])

    def tearDown(self):
        import app.news as news_mod

        news_mod.check_url = self._check

    def post(self, url, data, **kw):
        return self.admin.post(url, data={"csrf": self.csrf, **data}, follow_redirects=False, **kw)

    def test_stage_news_and_benefits_news(self):
        self.assertIn("Генератор новостей", self.admin.get("/admin/news/generate").text)
        r = self.post("/admin/news/generate", {"mode": "stages", "olympiads": [self.fiz.id], "start": "2026-11-01",
                                               "end": "2026-12-31", "with_benefits": "1", "cta": ["bot", "help"]})
        self.assertIn("/edit", r.headers["location"])
        edit = self.admin.get(r.headers["location"]).text
        self.assertIn("Онлайн-этап", edit)
        self.assertIn("Тестовый вуз", edit)                       # строка льгот
        self.assertIn("в боте Бвишечки", edit)                  # призыв
        with self.app.state.db() as s:
            item = s.news_list("draft")[0]
        self.assertEqual(item["category"], "stages")
        # без олимпиад — ошибка, без этапов в периоде — тоже
        self.assertIn("error", self.post("/admin/news/generate", {"mode": "stages"}).headers["location"])
        self.assertIn("error", self.post("/admin/news/generate", {"mode": "stages", "olympiads": [self.fiz.id],
                                                                 "start": "2027-06-01", "end": "2027-06-30"}
                                         ).headers["location"])
        b = self.post("/admin/news/generate", {"mode": "benefits", "university": "Тестовый вуз", "diploma": "winner"})
        self.assertIn("какие олимпиады дают льготы", self.admin.get(b.headers["location"]).text)

    def test_edit_sources_checked_before_publish(self):
        base = {"title": "Тестовый гайд 123", "body": "Текст", "category": "guides"}
        bad = self.post("/admin/news/save", {**base, "sources": "https://olimpiada.ru/x — агрегатор", "action": "draft"})
        self.assertIn("Не официальные источники", bad.text)
        broken = self.post("/admin/news/save", {**base, "sources": "https://olymp.example.ru/broken — Сайт",
                                                "action": "publish"})
        self.assertIn("Не открываются", broken.text)
        ok = self.post("/admin/news/save", {**base, "sources": "https://olymp.example.ru/ok — Сайт олимпиады",
                                            "action": "publish"})
        self.assertEqual(ok.status_code, 303)
        with self.app.state.db() as s:
            item = s.news_list("published")[0]
        page = self.anon.get(f"/news/{item['id']}", follow_redirects=True).text
        self.assertIn("Источники", page)
        self.assertIn("https://olymp.example.ru/ok", page)
        self.assertIn("Тестовый гайд 123", self.anon.get("/news?category=guides").text)
        self.assertNotIn("Тестовый гайд 123", self.anon.get("/news?category=stages").text)
        self.assertIn("Что такое БВИ", self.admin.get("/admin/news/new?template=0").text)


@unittest.skipUnless(HAS_WEB, "fastapi не установлен")
class StagesAdminTest(GeneratorTest):
    def test_stage_news_and_benefits_news(self):
        pass

    def test_edit_sources_checked_before_publish(self):
        pass

    def test_manual_stage_and_sync_request(self):
        from tests.helpers import sample_catalog

        other = [o for o in sample_catalog().olympiads.values() if o.id != self.fiz.id][0]
        page = self.admin.get("/admin/news/generate").text
        self.assertIn("этапов 2", page)                 # у Физтеха есть даты
        self.assertIn("нет дат", page)
        r = self.post("/admin/news/generate", {"mode": "stages", "olympiads": [other.id]})
        self.assertIn("error", r.headers["location"])
        self.post("/admin/stages/add", {"olympiad_id": other.id, "kind": "qualifying", "title": "Отбор-тест-77",
                                        "starts_at": "2026-12-13", "ends_at": ""})
        self.assertIn("Отбор-тест-77", self.admin.get(f"/admin/stages?olympiad={other.id}").text)
        ok = self.post("/admin/news/generate", {"mode": "stages", "olympiads": [other.id], "start": "2026-11-01",
                                                "end": "2026-12-31"})
        self.assertIn("/edit", ok.headers["location"])
        self.assertIn("13 декабря", self.admin.get(ok.headers["location"]).text)
        self.post("/admin/stages/sync", {})
        with self.app.state.db() as s:
            self.assertTrue(s.get_meta("stages_sync_request"))
            manual = [st for st in s.stages(other.id) if st.title == "Отбор-тест-77"][0]
        self.post(f"/admin/stages/{manual.id}/delete", {"olympiad": other.id})
        self.assertNotIn("Отбор-тест-77", self.admin.get(f"/admin/stages?olympiad={other.id}").text)


class HookStyleTest(unittest.TestCase):
    def test_hook_uses_real_deadline(self):
        from app.benefits import BenefitsCatalog, parse_file
        from app.news import GenOptions, stage_news
        from tests.helpers import sample_catalog
        from tests.test_benefits import university

        cat = sample_catalog()
        b = BenefitsCatalog([parse_file(university(), cat, "t.json")])
        o = cat.search("Физтех")[0].olympiad
        st = StageRow(olympiad_id=o.id, kind="registration", title="Регистрация", starts_at=date(2026, 11, 1),
                      ends_at=date(2026, 11, 6), profile=None, academic_year=cat.current_year, source_url=None)
        s = Storage(":memory:")
        s.upsert_stages([st])
        d = stage_news(cat, b, s.stages(), [o.id], [], {"registration"}, date(2026, 11, 1), date(2026, 11, 30),
                       GenOptions(style="hook", today=date(2026, 11, 4)), combined=False)[0]
        self.assertIn("закрывается через 2 дн.", d.title)
        self.assertIn("6 ноября", d.body)
