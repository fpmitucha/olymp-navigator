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
    def test_steps_and_deadline(self):
        from app.benefits import BenefitsCatalog, parse_file
        from app.crm import build_roadmap, registration_deadline
        from tests.helpers import sample_catalog
        from tests.test_benefits import university

        catalog = sample_catalog()
        benefits = BenefitsCatalog([parse_file(university(), catalog, "t.json")])
        fiztech = catalog.search("Физтех")[0].olympiad
        s = Storage(":memory:")
        s.upsert_stages([StageRow(olympiad_id=fiztech.id, kind="registration", title="Регистрация",
                                  starts_at=date(2026, 11, 1), ends_at=date(2026, 11, 20), profile=None,
                                  academic_year=catalog.current_year, source_url=None)])
        self.assertEqual(registration_deadline(s.stages(fiztech.id), fiztech.id, "математика", date(2026, 11, 4)),
                         date(2026, 11, 20))
        rows = [{"id": 1, "olympiad_id": fiztech.id, "profile": "математика", "status": "qualifying",
                 "reg_deadline": None}]
        goals = [{"id": 1, "university": "Тестовый вуз", "program": ""}]
        rm = build_roadmap(catalog, benefits, goals, rows, s.stages, date(2026, 11, 4))
        it = rm.items[0]
        self.assertEqual([st.state for st in it.steps], ["d", "n", "x", "x"])
        self.assertEqual(it.deadline, date(2026, 11, 20))
        self.assertTrue(it.benefits and it.benefits[0][0] == "Тестовый вуз")
        self.assertEqual((rm.done, rm.now, rm.ahead), (1, 1, 2))
        self.assertGreaterEqual(rm.goals[0].bvi + rm.goals[0].other, 1)


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
        self.app = create_app(self.catalog, benefits, admin_id=self.ADMIN, site_url="https://example.ru",
                              today=lambda: date(2026, 11, 4))
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
        post(f"{base}/settings", group_id=str(gid), notes="тайная заметка", notify_on="1")
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
        # выключить
        post(f"{base}/disable", confirm="выключить")
        self.assertEqual(anon.get(f"/r/{token}").status_code, 404)
