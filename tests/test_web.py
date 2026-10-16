import unittest

try:
    from fastapi.testclient import TestClient

    HAS_WEB = True
except ImportError:  # pragma: no cover
    HAS_WEB = False


@unittest.skipUnless(HAS_WEB, "fastapi не установлен")
class WebTest(unittest.TestCase):
    def setUp(self):
        from app.benefits import BenefitsCatalog, parse_file
        from app.web.main import create_app
        from tests.helpers import sample_catalog
        from tests.test_benefits import university

        self.catalog = sample_catalog()
        benefits = BenefitsCatalog([parse_file(university(), self.catalog, "t.json")])
        self.app = create_app(self.catalog, benefits)
        self.client = TestClient(self.app)
        self.fiztech = self.catalog.search("Физтех")[0].olympiad

    def test_pages(self):
        for url in ["/", "/olympiads", "/olympiads?level=1", "/universities", f"/olympiad/{self.fiztech.id}"]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn("Бвишечка", r.text)
        uni = self.client.get("/universities").text
        self.assertIn("Тестовый вуз", uni)

    def test_search_redirects_to_single_result(self):
        r = self.client.get("/olympiads?q=Физтех", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/olympiad/{self.fiztech.id}")

    def test_diploma_fit_and_pair_page(self):
        page = self.client.get(f"/olympiad/{self.fiztech.id}?profile=математика&diploma=winner&grade=11").text
        self.assertIn("Тестовый вуз", page)
        self.assertIn("БВИ", page)
        none = self.client.get(f"/olympiad/{self.fiztech.id}?profile=математика&diploma=winner&grade=10").text
        self.assertIn("ни один из собранных вузов", none)
        pair = self.client.get(f"/olympiad/{self.fiztech.id}/testovyy-vuz")
        self.assertEqual(pair.status_code, 200)
        self.assertIn("https://example.edu/olymp", pair.text)

    def test_pdf_and_api(self):
        pdf = self.client.get(f"/olympiad/{self.fiztech.id}/pdf?profile=математика")
        self.assertEqual(pdf.headers["content-type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        fit = self.client.get(f"/api/fit?olympiad={self.fiztech.id}&profile=математика&diploma=winner&grade=11").json()
        self.assertEqual(fit[0]["university"], "Тестовый вуз")

    def login(self, telegram_id: int = 77) -> str:
        """Вход через бота: страница даёт токен, бот подтверждает, сайт ставит сессию. Возвращает csrf."""
        import re

        page = self.client.get("/login").text
        token = re.search(r"token=([\w-]+)", page).group(1)
        self.assertFalse(self.client.get(f"/login/check?token={token}").json()["ok"])
        with self.app.state.db() as s:
            self.assertTrue(s.confirm_login(token, telegram_id))
        self.assertTrue(self.client.get(f"/login/check?token={token}").json()["ok"])
        finish = self.client.get(f"/login/finish?token={token}", follow_redirects=False)
        self.assertEqual(finish.headers["location"], "/me")
        self.client.cookies.set("bv_sid", finish.cookies["bv_sid"])
        # повторно тем же токеном войти нельзя
        self.assertEqual(self.client.get(f"/login/finish?token={token}", follow_redirects=False).headers["location"],
                         "/login")
        return re.search(r'name="csrf" value="(\w+)"', self.client.get("/me").text).group(1)

    def test_cabinet(self):
        self.assertEqual(self.client.get("/me", follow_redirects=False).headers["location"], "/login")
        csrf = self.login()
        post = lambda url, **data: self.client.post(url, data={"csrf": csrf, **data}, follow_redirects=False)
        self.assertEqual(post("/me/profile", name="  Катя  ", grade="11").status_code, 303)
        self.assertEqual(post("/me/diplomas", olympiad_id=self.fiztech.id, profile="математика", diploma="winner",
                              grade="11", year=self.catalog.current_year).status_code, 303)
        self.assertEqual(post("/me/universities/testovyy-vuz", action="follow").status_code, 303)
        me = self.client.get("/me").text
        self.assertIn("Катя, твой путь", me)
        self.assertIn("Тестовый вуз · БВИ", me)          # диплом сработал в вузе
        self.assertIn("Физтех", me)                       # и олимпиада добавилась в «слежу»
        with self.app.state.db() as s:
            self.assertEqual(len(s.subscriptions(77)), 1)   # подписка общая с ботом
            self.assertEqual(s.university_subscriptions(77), ["Тестовый вуз"])
        # чужая форма не проходит
        bad = self.client.post("/me/profile", data={"csrf": "x", "name": "Хакер"}, follow_redirects=False)
        self.assertEqual(bad.status_code, 403)
        # удалить данные
        self.assertEqual(post("/me/delete", confirm="удалить").status_code, 303)
        with self.app.state.db() as s:
            self.assertEqual((s.diplomas(77), s.subscriptions(77), s.profile(77)["name"]), ([], [], ""))

    def test_follow_requires_login(self):
        r = self.client.post(f"/me/olympiads/{self.fiztech.id}", data={"csrf": "", "action": "follow"},
                             follow_redirects=False)
        self.assertEqual(r.headers["location"], "/login")

    def test_not_found(self):
        self.assertEqual(self.client.get("/olympiad/nope").status_code, 404)
        self.assertEqual(self.client.get(f"/olympiad/{self.fiztech.id}/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
