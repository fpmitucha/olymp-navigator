import unittest

from app.benefits_watch import check_sources, year_links
from app.fetch import PageGone

PAGE_2026 = """<a href="/files/2026/olymp_benefits.pdf">Особые права олимпиадникам в 2026 году</a>
<a href="/news/2027">Новости 2027</a><a href="/rules">Правила приёма</a>"""
PAGE_2027 = PAGE_2026 + '<a href="/files/2027/olymp_benefits.pdf">Особые права победителям олимпиад, приём 2027</a>'


class FakeWeb:
    def __init__(self):
        self.pages = {"https://u.ru/": PAGE_2026, "https://m.ru/": "<p>поступающих в 2026 году</p>"}
        self.files = {"https://doc.ru/a.pdf": b"v1"}

    def html(self, url):
        return self.pages[url]

    def bytes(self, url):
        if url not in self.files:
            raise PageGone(url)
        return self.files[url]


SOURCES = [
    {"university": "У", "page": "https://u.ru/", "probe": ["https://u.ru/files/{year}/x.pdf"]},
    {"university": "М", "page": "https://m.ru/", "marker": "поступающих в {year} году"},
    {"university": "Д", "hash": ["https://doc.ru/a.pdf"]},
]


class WatchTest(unittest.TestCase):
    def test_year_links(self):
        links = year_links(PAGE_2027, "https://u.ru/", 2027)
        self.assertEqual(list(links), ["https://u.ru/files/2027/olymp_benefits.pdf"])  # «Новости 2027» — не про льготы

    def test_baseline_then_findings(self):
        web = FakeWeb()
        report, state = check_sources(SOURCES, 2027, {}, web.html, web.bytes)
        self.assertTrue(report.baseline)
        self.assertEqual(report.findings, [])

        report, state = check_sources(SOURCES, 2027, state, web.html, web.bytes)
        self.assertEqual(report.findings, [])  # ничего не изменилось

        web.pages["https://u.ru/"] = PAGE_2027
        web.files["https://u.ru/files/2027/x.pdf"] = b"pdf"
        web.pages["https://m.ru/"] = "<p>поступающих в 2027 году</p>"
        web.files["https://doc.ru/a.pdf"] = b"v2"
        report, state = check_sources(SOURCES, 2027, state, web.html, web.bytes)
        found = {(f.university, f.url) for f in report.findings}
        self.assertEqual(found, {
            ("У", "https://u.ru/files/2027/olymp_benefits.pdf"), ("У", "https://u.ru/files/2027/x.pdf"),
            ("М", "https://m.ru/"), ("Д", "https://doc.ru/a.pdf")})

        report, _ = check_sources(SOURCES, 2027, state, web.html, web.bytes)
        self.assertEqual(report.findings, [])  # о найденном второй раз не сообщаем

    def test_site_down_is_not_fatal(self):
        def broken(url):
            raise RuntimeError("timeout")

        report, state = check_sources(SOURCES[:1], 2027, {"У": {}}, broken, FakeWeb().bytes)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.findings, [])


if __name__ == "__main__":
    unittest.main()
