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
        self.client = TestClient(create_app(self.catalog, benefits))
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
        self.assertIn("Ни один из собранных вузов", none)
        pair = self.client.get(f"/olympiad/{self.fiztech.id}/testovyy-vuz")
        self.assertEqual(pair.status_code, 200)
        self.assertIn("https://example.edu/olymp", pair.text)

    def test_pdf_and_api(self):
        pdf = self.client.get(f"/olympiad/{self.fiztech.id}/pdf?profile=математика")
        self.assertEqual(pdf.headers["content-type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        fit = self.client.get(f"/api/fit?olympiad={self.fiztech.id}&profile=математика&diploma=winner&grade=11").json()
        self.assertEqual(fit[0]["university"], "Тестовый вуз")

    def test_not_found(self):
        self.assertEqual(self.client.get("/olympiad/nope").status_code, 404)
        self.assertEqual(self.client.get(f"/olympiad/{self.fiztech.id}/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
