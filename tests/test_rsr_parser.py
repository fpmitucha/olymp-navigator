"""Тесты парсера перечня РСОШ. Запуск: python -m unittest discover -s tests -v"""

from __future__ import annotations

import unittest
from pathlib import Path

from app.parsers.rsr import (
    RsrList,
    RsrParseError,
    detect_academic_year,
    olympiad_key,
    parse_rsr_html,
    rsr_url_for_year,
)
from app.parsers.subjects import split_subjects

FIXTURE = Path(__file__).parent / "fixtures" / "rsr_2026_sample.html"
PHOTONICS = "фотоника, приборостроение, оптические и биотехнические системы и технологии"
GEOLOGY = "прикладная геология, горное дело, нефтегазовое дело и геодезия"


class SplitSubjectsTest(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(split_subjects("история, обществознание"), (["история", "обществознание"], []))

    def test_compound_names_are_not_split(self):
        subjects, warnings = split_subjects(
            "информатика и вычислительная техника, электроника, радиотехника и системы связи, "
            f"{PHOTONICS}, управление в технических системах"
        )
        self.assertEqual(subjects, [
            "информатика и вычислительная техника",
            "электроника, радиотехника и системы связи",
            PHOTONICS,
            "управление в технических системах",
        ])
        self.assertEqual(warnings, [])

    def test_short_variants_become_canonical(self):
        subjects, _ = split_subjects("фотоника, приборостроение, электро- и теплоэнергетика")
        self.assertEqual(subjects, [PHOTONICS, "электро- и теплоэнергетика"])
        subjects, _ = split_subjects("науки о Земле, прикладная геология, горное дело, нефтегазовое дело")
        self.assertEqual(subjects, ["науки о Земле", GEOLOGY])

    def test_archive_2022_variants(self):
        # формулировки из перечня 2022/23
        cases = [
            "оптические и биотехнические системы и технологии, ядерная энергетика и технологии, машиностроение",
            "электроника, радиотехника и системы связи, приборостроение, оптические и биотехнические системы "
            "и технологии, технологии материалов",
            "естественные науки, инженерные науки, приборостроение, ядерная энергетика и технологии",
        ]
        for raw in cases:
            subjects, warnings = split_subjects(raw)
            self.assertEqual(warnings, [], raw)
            self.assertIn(PHOTONICS, subjects)
            self.assertNotIn("приборостроение", subjects)
        subjects, _ = split_subjects(cases[1])
        self.assertEqual(subjects, ["электроника, радиотехника и системы связи", PHOTONICS, "технологии материалов"])

    def test_archive_2023_missing_comma(self):
        raw = (
            "биологические науки, информационная безопасность, информатика и вычислительная техника, "
            "информационная безопасность, машиностроение, фотоника, приборостроение, оптические и биотехнические "
            "системы и технологии электроника, радиотехника и системы связи, ядерная энергетика и технологии"
        )
        subjects, warnings = split_subjects(raw)
        self.assertEqual(warnings, [])
        self.assertEqual(subjects, [
            "биологические науки", "информационная безопасность", "информатика и вычислительная техника",
            "машиностроение", PHOTONICS, "электроника, радиотехника и системы связи", "ядерная энергетика и технологии",
        ])

    def test_architecture_is_two_subjects(self):
        subjects, _ = split_subjects("архитектура, изобразительное и прикладные виды искусств")
        self.assertEqual(subjects, ["архитектура", "изобразительное и прикладные виды искусств"])

    def test_unknown_compound_produces_warning(self):
        subjects, warnings = split_subjects("горное дело, физика")
        self.assertEqual(subjects, ["горное дело", "физика"])
        self.assertEqual(len(warnings), 1)

    def test_duplicates_and_spaces(self):
        subjects, _ = split_subjects("  физика ,физика,  ,математика  ")
        self.assertEqual(subjects, ["физика", "математика"])


class OlympiadKeyTest(unittest.TestCase):
    def test_roman_numeral_removed(self):
        self.assertEqual(
            olympiad_key("XIX Южно-Российская олимпиада «Архитектура и искусство»"),
            olympiad_key("XVIII Южно-Российская олимпиада «Архитектура и искусство»"),
        )

    def test_cyrillic_first_letter_kept(self):
        # «М» в «Московская» — кириллица, не римская цифра
        self.assertTrue(olympiad_key("Московская олимпиада школьников").startswith("московская"))


class AcademicYearTest(unittest.TestCase):
    def test_from_archive_url(self):
        self.assertEqual(detect_academic_year("", "https://rsr-olymp.ru/archive/2024"), "2024/25")

    def test_last_mention_before_table_wins(self):
        html = (
            "<a>Утвержден перечень олимпиад школьников и их уровней на 2026/27 учебный год</a>"
            "<p>Приказ «Об утверждении перечня олимпиад школьников и их уровней на 2025/26 учебный год»</p>"
            '<table class="mainTableInfo"></table><p>на 2030/31 учебный год</p>'
        )
        self.assertEqual(detect_academic_year(html), "2025/26")

    def test_inconsistent_year_rejected(self):
        self.assertIsNone(detect_academic_year("<p>на 2026/29 учебный год</p><table class='mainTableInfo'>"))

    def test_urls(self):
        self.assertEqual(rsr_url_for_year(None), "https://rsr-olymp.ru/")
        self.assertEqual(rsr_url_for_year(2023), "https://rsr-olymp.ru/archive/2023")


class ParseFixtureTest(unittest.TestCase):
    rsr: RsrList

    @classmethod
    def setUpClass(cls):
        cls.rsr = parse_rsr_html(FIXTURE.read_text(encoding="utf-8"), url="https://rsr-olymp.ru/")

    def olympiad(self, number: int):
        return next(o for o in self.rsr.olympiads if o.number == number)

    def test_year_and_counts(self):
        self.assertEqual(self.rsr.academic_year, "2026/27")
        self.assertEqual(len(self.rsr.olympiads), 11)
        self.assertEqual(self.rsr.profile_count, 24)
        self.assertEqual(self.rsr.warnings, [])

    def test_rowspan_profiles_belong_to_olympiad(self):
        nto = self.olympiad(3)
        self.assertEqual(
            [p.profile for p in nto.profiles],
            ["инфохимия", "летающая робототехника", "беспилотные авиационные системы",
             "инженерные биологические системы"],
        )
        self.assertTrue(all(p.level == 3 for p in nto.profiles))
        self.assertTrue(all(p.website == "http://ntcontest.ru" for p in nto.profiles))
        self.assertIn("сельское, лесное и рыбное хозяйство", nto.profiles[3].subjects)

    def test_same_number_different_sites_merged(self):
        rosatom = self.olympiad(52)
        self.assertEqual([p.profile for p in rosatom.profiles], ["математика", "физика", "информатика"])
        self.assertEqual(
            [p.website for p in rosatom.profiles],
            ["https://olymp.mephi.ru/rosatom_it/", "https://olymp.mephi.ru/rosatom_it/",
             "https://olymp.mephi.ru/rosatom/about"],
        )
        spb = self.olympiad(55)
        self.assertEqual([(p.profile, p.level) for p in spb.profiles],
                         [("математика", 1), ("физика", 2), ("химия", 1)])

    def test_levels_and_subjects(self):
        zvezda = self.olympiad(25)
        natural = zvezda.profiles[0]
        self.assertEqual(natural.level, 2)
        self.assertIn(PHOTONICS, natural.subjects)
        self.assertIn(GEOLOGY, natural.subjects)
        self.assertIn("электроника, радиотехника и системы связи", natural.subjects)
        self.assertNotIn("приборостроение", natural.subjects)
        self.assertEqual(self.olympiad(27).profiles[0].subjects, ["науки о Земле", GEOLOGY])
        self.assertEqual(self.olympiad(6).profiles[1].subjects, ["философия, этика и религиоведение"])

    def test_website_cleanup(self):
        self.assertEqual(self.olympiad(43).profiles[0].website, "http://www.olympiada.spbu.ru/")
        self.assertEqual(self.olympiad(26).profiles[0].website, "https://мош.цпм.рф")

    def test_roundtrip_dict(self):
        again = RsrList.from_dict(self.rsr.to_dict())
        self.assertEqual(again.to_dict(), self.rsr.to_dict())


class ParseErrorsTest(unittest.TestCase):
    def test_no_table(self):
        with self.assertRaises(RsrParseError):
            parse_rsr_html("<html><body>Сайт на обслуживании</body></html>", academic_year="2026/27")

    def test_bad_level_is_reported(self):
        html = (
            '<table class="mainTableInfo"><tr><td>1</td><td><a href="x">Олимпиада</a></td>'
            "<td>математика</td><td>математика</td><td>4</td></tr></table>"
        )
        rsr = parse_rsr_html(html, academic_year="2026/27")
        self.assertIsNone(rsr.olympiads[0].profiles[0].level)
        self.assertEqual(len(rsr.warnings), 1)


if __name__ == "__main__":
    unittest.main()
