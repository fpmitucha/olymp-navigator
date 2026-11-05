import asyncio
import re
import unittest
from datetime import date

from app.bot.storage import Storage
from app.stages import StageRow

try:
    from fastapi.testclient import TestClient

    HAS_WEB = True
except ImportError:  # pragma: no cover
    HAS_WEB = False


class CrmStorageTest(unittest.TestCase):
    def setUp(self):
        self.s = Storage(":memory:")

    def test_code_is_stable_and_unique(self):
        a, b = self.s.user_code(1), self.s.user_code(2)
        self.assertRegex(a, r"^\d{6}$")
        self.assertNotEqual(a, b)
        self.assertEqual(self.s.user_code(1), a)
        self.assertEqual(self.s.user_by_code(f"{a[:3]} {a[3:]}"), 1)
        self.assertIsNone(self.s.user_by_code("000"))

    def test_client_lifecycle(self):
        self.assertTrue(self.s.crm_enable(5))
        self.assertFalse(self.s.crm_enable(5))
        gid = self.s.crm_add_group("11 класс")
        self.s.crm_update_client(5, group_id=gid, notes="заметка")
        self.assertEqual(self.s.crm_client(5)["group_name"], "11 класс")
        self.assertEqual(self.s.crm_group_members(gid), [5])
        self.assertTrue(self.s.crm_add_olympiad(5, "o1", "математика"))
        self.assertFalse(self.s.crm_add_olympiad(5, "o1", "математика"))
        row = self.s.crm_olympiads(5)[0]
        self.s.crm_set_status(5, row["id"], "passed")
        self.s.crm_set_deadline(5, row["id"], date(2026, 11, 17))
        row = self.s.crm_olympiad(5, row["id"])
        self.assertEqual((row["status"], row["reg_deadline"]), ("passed", date(2026, 11, 17)))
        self.assertIsNone(self.s.crm_olympiad(6, row["id"]))   # чужую строку не видно
        token = self.s.crm_client(5)["share_token"]
        self.assertEqual(self.s.crm_client_by_token(token), 5)
        self.s.crm_disable(5)
        self.assertEqual(self.s.crm_olympiads(5), [])           # всё удалилось каскадом

    def test_outbox(self):
        from app.bot.notifier import UserBlocked, send_outbox

        self.s.queue_message(1, "a")
        self.s.queue_message(2, "b")
        sent = []

        async def send(tid, text):
            if tid == 2:
                raise UserBlocked("x")
            sent.append((tid, text))

        import app.bot.notifier as n
        pause, n.SEND_PAUSE = n.SEND_PAUSE, 0
        try:
            asyncio.run(send_outbox(self.s, send))
        finally:
            n.SEND_PAUSE = pause
        self.assertEqual(sent, [(1, "a")])
        self.assertEqual(self.s.pending_messages(), [])


class RoadmapTest(unittest.TestCase):
    def setUp(self):
        from app.benefits import BenefitsCatalog, parse_file
        from tests.helpers import sample_catalog
        from tests.test_benefits import university

        self.catalog = sample_catalog()
        self.benefits = BenefitsCatalog([parse_file(university(), self.catalog, "t.json")])
        self.fiztech = self.catalog.search("Физтех")[0].olympiad
        self.s = Storage(":memory:")

    def stage(self, kind, title, start, end, profile=None, n=0):
        return StageRow(olympiad_id=self.fiztech.id, kind=kind, title=title, starts_at=start, ends_at=end,
                        profile=profile, academic_year=self.catalog.current_year, source_url=None,
                        external_id=f"olimpiada:1:{n}")

    def test_steps_deadline_and_two_qualifying_stages(self):
        from app.crm import build_roadmap

        self.s.upsert_stages([
            self.stage("registration", "Регистрация", date(2026, 11, 1), date(2026, 11, 20), n=1),
            self.stage("qualifying", "Первый отборочный этап", date(2026, 11, 22), date(2026, 11, 23), "математика", 2),
            self.stage("qualifying", "Второй отборочный этап", date(2026, 12, 13), date(2026, 12, 14),
                       "математика, физика", 3),
            self.stage("qualifying", "Отбор по химии", date(2026, 12, 1), None, "химия", 4),
            self.stage("final", "Заключительный этап", date(2027, 2, 20), None, n=5),
        ])
        rows = [{"id": 1, "olympiad_id": self.fiztech.id, "profile": "математика", "status": "q2",
                 "reg_deadline": None, "plan": "[]"}]
        goals = [{"id": 1, "university": "Тестовый вуз", "program": ""}]
        rm = build_roadmap(self.catalog, self.benefits, goals, rows, self.s.stages, date(2026, 11, 4))
        it = rm.items[0]
        self.assertEqual(it.n, 2)                                          # химия не наша
        self.assertEqual([st.name for st in it.steps],
                         ["Регистрация", "1-й отборочный", "2-й отборочный", "Финал", "Итог"])
        self.assertEqual([st.state for st in it.steps], ["d", "d", "n", "x", "x"])
        self.assertEqual([st.when for st in it.steps][1:4], ["22.11–23.11", "13.12–14.12", "20.02"])
        self.assertEqual(it.label, "Идёт 2-й отборочный")
        self.assertEqual(it.deadline, date(2026, 11, 20))
        self.assertEqual(it.access_deadline, date(2026, 11, 17))
        self.assertTrue(it.benefits and it.benefits[0][0] == "Тестовый вуз")
        self.assertIsNotNone(it.plan_changed)
        # прошедшие этапы пропали с сайта — снимок их сохранил
        rows[0]["plan"] = it.plan_changed
        rm2 = build_roadmap(self.catalog, self.benefits, goals, rows, lambda _: [], date(2026, 12, 20))
        self.assertEqual(rm2.items[0].n, 2)
        self.assertIsNone(rm2.items[0].plan_changed)

    def test_statuses(self):
        from app.crm import normalize_status, progress, status_label, status_options, valid_status

        self.assertEqual(progress("qualifying", 1), (0, 1, None))           # статус первой версии
        self.assertEqual(progress("passed", 2), (2, None, None))
        self.assertEqual(progress("f2", 2), (1, None, 2))
        self.assertEqual(status_label("q1ok", 2), "1-й отборочный пройден")
        self.assertEqual(status_label("q2ok", 2), "Отбор пройден")
        self.assertEqual(status_label("f1", 1), "Отбор не пройден")
        self.assertEqual(normalize_status("passed", 2), "q2ok")
        self.assertIn(("q2", "Идёт 2-й отборочный"), status_options(2))
        self.assertTrue(valid_status("q3ok") and not valid_status("f1ok") and not valid_status("x"))

    def test_access_reminders(self):
        from app.crm import access_reminders

        rows = [{"id": 7, "telegram_id": 5, "olympiad_id": self.fiztech.id, "profile": "", "status": "registration",
                 "reg_deadline": date(2026, 11, 20), "plan": "[]", "access": None}]
        self.assertEqual(access_reminders(self.catalog, rows, lambda _: [], date(2026, 11, 13)), [])
        found = access_reminders(self.catalog, rows, lambda _: [], date(2026, 11, 14))
        self.assertEqual([(r[0], r[1], r[2]) for r in found], [(7, 5, "access_t3")])
        rows[0]["access"] = "pending"
        self.assertEqual(access_reminders(self.catalog, rows, lambda _: [], date(2026, 11, 14)), [])


@unittest.skipUnless(HAS_WEB, "fastapi не установлен")
class CrmWebTest(unittest.TestCase):
    ADMIN, CLIENT = 1000, 77

    def setUp(self):
        from app.benefits import BenefitsCatalog, parse_file
        from app.web.main import create_app
        from tests.helpers import sample_catalog
        from tests.test_benefits import university

        self.catalog = sample_catalog()
        benefits = BenefitsCatalog([parse_file(university(), self.catalog, "t.json")])
        from app.secretbox import SecretBox

        self.app = create_app(self.catalog, benefits, admin_id=self.ADMIN, site_url="https://example.ru",
                              today=lambda: date(2026, 11, 4), crm_key=SecretBox.new_key())
        self.fiztech = self.catalog.search("Физтех")[0].olympiad

    def login(self, telegram_id: int) -> tuple[TestClient, str]:
        client = TestClient(self.app)
        page = client.get("/login").text
        token = re.search(r"token=([\w-]+)", page).group(1)
        with self.app.state.db() as s:
            s.confirm_login(token, telegram_id)
        finish = client.get(f"/login/finish?token={token}", follow_redirects=False)
        client.cookies.set("bv_sid", finish.cookies["bv_sid"])
        csrf = re.search(r'name="csrf" value="(\w+)"', client.get("/me").text).group(1)
        return client, csrf

    def test_full_flow(self):
        client, ccsrf = self.login(self.CLIENT)
        me = client.get("/me").text
        code = re.search(r"Твой ID</span> (\d{3}) (\d{3})", me)
        code = code.group(1) + code.group(2)
        self.assertEqual(client.get("/me/roadmap", follow_redirects=False).headers["location"], "/me")
        self.assertEqual(client.get("/admin").status_code, 404)        # клиент не видит админку
        # обычный пользователь не может открыть сопровождение
        self.assertEqual(client.post("/admin/open", data={"csrf": ccsrf, "code": code}).status_code, 404)

        admin, csrf = self.login(self.ADMIN)
        post = lambda url, **data: admin.post(url, data={"csrf": csrf, **data}, follow_redirects=False)
        self.assertIn("Админка", admin.get("/").text)
        miss = post("/admin/open", code="999999").headers["location"]
        self.assertIn("error=", miss)
        self.assertIn("Нет пользователя с таким ID", admin.get(miss).text)
        r = post("/admin/open", code=f"{code[:3]} {code[3:]}")
        self.assertEqual(r.headers["location"], f"/admin/client/{code}")
        base = f"/admin/client/{code}"
        post("/admin/groups", name="11 класс, ноябрь")
        with self.app.state.db() as s:
            gid = s.crm_groups()[0]["id"]
        page = admin.get(base).text
        self.assertIn("Без имени", page)
        self.assertIn('class="pp"', page)                                # вуз + поиск по программам
        self.assertIn("Тестовый вуз", page)
        self.assertRegex(admin.get("/").text, r'/static/style\.css\?v=\w{10}')
        self.assertIn("bv-", admin.get("/sw.js").text)
        post(f"{base}/settings", group_id=str(gid), notes="тайная заметка", notify_on="1",
             display_name="  Маша   Иванова ", grade="10")
        self.assertIn("Маша Иванова", admin.get("/admin").text)            # имя задал куратор
        self.assertIn("10 класс", admin.get(base).text)
        self.assertIn("Маша Иванова", client.get("/me/roadmap").text)       # клиент видит то же имя
        post(f"{base}/settings", group_id=str(gid), notes="тайная заметка", notify_on="1", grade="99")
        with self.app.state.db() as s:
            c = s.crm_client(self.CLIENT)
            self.assertEqual((c["display_name"], c["grade_override"]), ("", None))
        post(f"{base}/settings", group_id=str(gid), notes="тайная заметка", notify_on="1", display_name="Маша")
        post(f"{base}/goals", university="Тестовый вуз", program="")
        self.assertEqual(post(f"{base}/goals", university="Нет такого").status_code, 422)
        hits = admin.get(f"{base}?q=Физтех").text
        self.assertIn(self.fiztech.id, hits)
        post(f"{base}/olympiads", olympiad_id=self.fiztech.id, profile="математика")
        with self.app.state.db() as s:
            row = s.crm_olympiads(self.CLIENT)[0]
            self.assertIn(self.fiztech.id, s.subscriptions(self.CLIENT))   # напоминания бота включены
        self.assertEqual(post(f"{base}/olympiads/{row['id']}/status", status="nope").status_code, 422)
        post(f"{base}/olympiads/{row['id']}/status", status="passed")
        post(f"{base}/olympiads/{row['id']}/deadline", deadline="2026-11-17")
        post(f"{base}/message", text="Не забудь <онлайн-тур>")
        card = admin.get(base).text
        self.assertIn("тайная заметка", card)
        self.assertIn("Отбор пройден", card)
        self.assertIn("задано вручную", card)
        with self.app.state.db() as s:
            texts = [m["text"] for m in s.pending_messages()]
        self.assertTrue(any("Отбор пройден" in t and "https://example.ru/me/roadmap" in t for t in texts))
        self.assertTrue(any("&lt;онлайн-тур&gt;" in t for t in texts))     # текст куратора экранирован
        self.assertIn("11 класс, ноябрь", admin.get(f"/admin?group={gid}").text)

        # клиент видит роадмап, но не заметки
        roadmap = client.get("/me/roadmap").text
        self.assertIn("Роадмап по олимпиадам", roadmap)
        self.assertIn("Отбор пройден", roadmap)
        self.assertIn("Тестовый вуз", roadmap)
        self.assertNotIn("тайная заметка", roadmap)
        self.assertIn("Моё сопровождение", client.get("/me").text)
        # ссылка для родителей — без входа
        token = re.search(r"/r/([\w-]+)", roadmap).group(1)
        anon = TestClient(self.app)
        shared = anon.get(f"/r/{token}")
        self.assertEqual(shared.status_code, 200)
        self.assertIn("Отбор пройден", shared.text)
        self.assertNotIn("Ссылка для родителей", shared.text)
        self.assertEqual(anon.get("/r/wrong").status_code, 404)
        # клиент не может менять статусы
        self.assertEqual(client.post(f"{base}/olympiads/{row['id']}/status",
                                     data={"csrf": ccsrf, "status": "winner"}).status_code, 404)
        # клиент вводит доступ к кабинету олимпиады
        post(f"{base}/olympiads/{row['id']}/status", status="registration")
        self.assertIn("Ввести логин и пароль", client.get("/me/roadmap").text)
        bad = client.post(f"/me/roadmap/{row['id']}/access", data={"csrf": ccsrf, "login": "m@x.ru", "password": "p"},
                          follow_redirects=False)
        self.assertIn("access_error", bad.headers["location"])                # без согласия не берём
        client.post(f"/me/roadmap/{row['id']}/access",
                    data={"csrf": ccsrf, "login": "m@x.ru", "password": "секрет-42", "consent": "1"})
        with self.app.state.db() as s:
            acc = s.crm_accesses(self.CLIENT)[0]
        self.assertNotIn("секрет", acc["password_enc"])                       # в базе шифротекст
        self.assertIn("На проверке", client.get("/me/roadmap").text)
        self.assertNotIn("секрет-42", anon.get(f"/r/{token}").text)
        queue = admin.get("/admin/access").text
        self.assertIn("m@x.ru", queue)
        self.assertNotIn("секрет-42", queue)                                   # пароль скрыт до «Показать»
        self.assertEqual(client.get("/admin/access").status_code, 404)
        shown = post(f"/admin/access/{acc['id']}/reveal")
        self.assertIn("секрет-42", shown.text)
        post(f"/admin/access/{acc['id']}/check", result="ok")
        self.assertIn("просмотрен пароль", admin.get("/admin/access").text)  # журнал просмотров
        self.assertIn("Проверено", client.get("/me/roadmap").text)
        with self.app.state.db() as s:
            self.assertTrue(any("Доступ проверен" in m["text"] for m in s.pending_messages()))
        # выключить
        post(f"{base}/disable", confirm="выключить")
        self.assertEqual(anon.get(f"/r/{token}").status_code, 404)


@unittest.skipUnless(HAS_WEB, "fastapi не установлен")
class GoalsFromListTest(CrmWebTest):
    def test_full_flow(self):  # не повторяем большой сценарий
        pass

    def test_add_university_from_list_and_cabinet(self):
        client, csrf = self.login(self.CLIENT)
        self.assertIn("В мои цели", client.get("/universities").text)
        client.post("/me/universities", data={"csrf": csrf, "university": "Тестовый вуз"})
        with self.app.state.db() as s:
            self.assertEqual(s.university_subscriptions(self.CLIENT), ["Тестовый вуз"])
        self.assertIn("В целях", client.get("/universities").text)


class SessionTest(unittest.TestCase):
    def test_sliding_30_days(self):
        s = Storage(":memory:")
        s.touch_user(1)
        s.create_session("sid", 1)
        self.assertEqual(s.session_user("sid"), 1)
        old = "2000-01-01T00:00:00+00:00"
        s.conn.execute("UPDATE web_session SET created_at = ?, last_seen = ?", (old, old))
        self.assertIsNone(s.session_user("sid"))                      # 30 дней без заходов — выход
        recent = __import__("app.bot.storage", fromlist=["_ago"])._ago(days=29)
        s.conn.execute("UPDATE web_session SET last_seen = ?", (recent,))
        self.assertEqual(s.session_user("sid"), 1)                     # заходил недавно — сессия жива
        self.assertGreater(s.conn.execute("SELECT last_seen FROM web_session").fetchone()[0], recent)  # продлилась
