from __future__ import annotations

import unittest

from app.catalog import Catalog, _fuzzy_name, dedupe_urls, short_id
from app.parsers.rsr import olympiad_key
from tests.helpers import olympiad, profile, rsr_list, sample_catalog


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.c = sample_catalog()

    def test_counts_and_years(self):
        self.assertEqual(self.c.years, ["2026/27", "2025/26", "2024/25"])
        self.assertEqual(self.c.current_year, "2026/27")
        # Физтех, НТО, 2× «Архитектура и искусство», Робофест, ОММО, Ломоносов, ВсОШ
        self.assertEqual(len(self.c.olympiads), 8)

    def test_renamed_olympiad_is_merged(self):
        nto = self.c.search("нто")
        self.assertEqual(len(nto), 1)
        self.assertEqual(nto[0].olympiad.years, ["2026/27", "2025/26", "2024/25"])
        self.assertIn("8–11 классов", nto[0].olympiad.name)  # название из свежего перечня

    def test_manual_rename(self):
        ommo = self.c.search("оммо")
        self.assertEqual(len(ommo), 1)
        self.assertEqual(ommo[0].olympiad.years, ["2026/27", "2025/26", "2024/25"])

    def test_same_name_same_years_not_merged(self):
        arch = [h.olympiad for h in self.c.search("архитектура и искусство")]
        self.assertEqual(len(arch), 2)
        self.assertEqual(sorted(len(o.years) for o in arch), [2, 3])

    def test_old_ids_still_resolve(self):
        old_key = olympiad_key("Объединенная межвузовская математическая олимпиада школьников")
        ommo = self.c.get(short_id(old_key))
        self.assertIsNotNone(ommo)
        self.assertEqual(self.c.canonical_id(short_id(old_key)), ommo.id)
        self.assertIs(self.c.by_key(old_key), ommo)
        self.assertEqual(self.c.canonical_id("неизвестный"), "неизвестный")

    def test_fuzzy_name(self):
        self.assertEqual(
            _fuzzy_name("Всероссийская Толстовская олимпиада школьников"),
            _fuzzy_name("Всероссийская Толстовская олимпиада"),
        )
        self.assertEqual(
            _fuzzy_name("«Формула Единства» / «Третье тысячелетие»"),
            _fuzzy_name("«Формула Единства»/«Третье тысячелетие»"),
        )
        self.assertNotEqual(_fuzzy_name("Открытая олимпиада школьников"),
                            _fuzzy_name("Открытая олимпиада школьников по программированию"))

    def test_dedupe_urls(self):
        self.assertEqual(
            dedupe_urls(["https://olymp.mipt.ru", "https://olymp.mipt.ru/", "http://www.olymp.mipt.ru", "x.ru"]),
            ["https://olymp.mipt.ru", "x.ru"],
        )

    def test_current_year_ignores_vsosh(self):
        from app.parsers.vsosh import vsosh_list

        lists = [rsr_list("2025/26", olympiad("Олимпиада «Тест»", profile("физика", 1)))]
        c = Catalog(lists, [vsosh_list("2026/27")])
        self.assertEqual(c.years, ["2025/26"])  # перечень РСОШ на 2026/27 ещё не скачан
        self.assertEqual(c.current_year, "2025/26")


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.c = sample_catalog()

    def names(self, query):
        return [h.olympiad.name for h in self.c.search(query)]

    def test_alias_exact(self):
        hits = self.c.search("ВсОШ")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].olympiad.is_vsosh)

    def test_subject_search_ranks_vsosh_then_level(self):
        hits = self.c.search("физика")
        self.assertTrue(hits[0].olympiad.is_vsosh)
        names = [h.olympiad.name for h in hits]
        self.assertLess(names.index("Олимпиада школьников «Физтех»"), names.index("Объединенная межвузовская олимпиада школьников"))
        self.assertEqual(names[-1], "Олимпиада школьников «Робофест»")  # нет в текущем перечне

    def test_name_plus_subject(self):
        hits = self.c.search("ломоносов химия")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].matched_profiles, ["химия"])
        self.assertFalse(hits[0].name_match)

    def test_alias_inside_query(self):
        hits = self.c.search("нто искусственный интеллект")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].matched_profiles, ["искусственный интеллект"])

    def test_matches_subjects(self):
        hits = self.c.search("машиностроение")
        self.assertEqual([h.matched_profiles for h in hits], [["летающая робототехника"]])

    def test_words_from_different_profiles_do_not_match(self):
        self.assertEqual(self.c.search("математика химия физика"), [])

    def test_case_and_yo(self):
        self.assertEqual(self.names("ФИЗТЕХ"), ["Олимпиада школьников «Физтех»"])
        self.assertEqual(self.c.search(""), [])
        self.assertEqual(self.c.search("   "), [])

    def test_limit(self):
        self.assertEqual(len(self.c.search("олимпиада", limit=3)), 3)

    def test_best_level_and_history(self):
        fiztech = self.c.search("физтех")[0].olympiad
        self.assertEqual(fiztech.best_level("2026/27"), 1)
        self.assertEqual(fiztech.profiles["математика"]["2025/26"].level, 2)


if __name__ == "__main__":
    unittest.main()
