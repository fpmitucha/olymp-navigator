"""Льготы вузов: формат файлов, проверка, выборка, тексты."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.benefits import BenefitsError, load_benefits, parse_file
from app.bot import texts
from app.config import PROJECT_ROOT
from tests.helpers import sample_catalog


def university(**overrides):
    data = {
        "university": "Тестовый вуз", "full_name": "Тестовый университет", "city": "Москва",
        "admission_year": 2027, "document_status": "approved",
        "source_url": "https://example.edu/olymp", "source_title": "Особые права", "checked_at": "2026-10-15",
        "notes": ["Нужен ЕГЭ от 75."],
        "defaults": {"confirm_min_score": 75, "grades": [11]},
        "rules": [
            {"olympiad": "Физтех", "benefit": "bvi", "diploma": "winner", "profiles": ["математика"], "levels": [1]},
            {"olympiad": "Олимпиада школьников «Физтех»", "benefit": "score100", "diploma": "prize"},
            {"olympiad": "ВсОШ", "benefit": "bvi", "diploma": "any", "confirm_min_score": None, "grades": None},
        ],
    }
    data.update(overrides)
    return data


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.c = sample_catalog()

    def test_valid(self):
        u = parse_file(university(), self.c, "test.json")
        self.assertEqual(len(u.rules), 3)
        bvi = u.rules[0]
        self.assertEqual((bvi.benefit, bvi.diploma, bvi.profiles, bvi.levels), ("bvi", "winner", ["математика"], [1]))
        self.assertEqual((bvi.confirm_min_score, bvi.grades), (75, [11]))  # из defaults
        self.assertIs(u.rules[1].olympiad, bvi.olympiad)                   # разные написания — одна олимпиада
        vsosh = u.rules[2]
        self.assertTrue(vsosh.olympiad.is_vsosh)
        self.assertIsNone(vsosh.confirm_min_score)                         # правило переопределило defaults

    def test_errors_are_collected(self):
        data = university(document_status="final", rules=[
            {"olympiad": "Несуществующая олимпиада", "benefit": "bvi"},
            {"olympiad": "Физтех", "benefit": "free", "diploma": "everyone"},
            {"olympiad": "Физтех", "benefit": "bvi", "profiles": ["астрология"], "levels": [4]},
            {"olympiad": "Физтех", "benefit": "bvi", "confirm_min_score": 150},
        ])
        with self.assertRaises(BenefitsError) as ctx:
            parse_file(data, self.c, "bad.json")
        message = str(ctx.exception)
        for fragment in ("document_status", "не найдена", "benefit", "diploma", "астрология", "уровни",
                         "confirm_min_score"):
            self.assertIn(fragment, message)

    def test_missing_fields(self):
        with self.assertRaises(BenefitsError) as ctx:
            parse_file({"rules": []}, self.c, "empty.json")
        self.assertIn("university", str(ctx.exception))

    def test_latest_year_wins_and_lookup(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "a_2026.json").write_text(json.dumps(university(admission_year=2026)), encoding="utf-8")
        (folder / "a_2027.json").write_text(json.dumps(university(document_status="draft")), encoding="utf-8")
        b = load_benefits(folder, self.c)
        latest = b.latest()
        self.assertEqual([(u.university, u.admission_year) for u in latest], [("Тестовый вуз", 2027)])
        fiztech = self.c.search("физтех")[0].olympiad
        entries = b.for_olympiad(fiztech.id)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0][1]), 2)
        self.assertEqual(latest[0].status_label(2027), "приём 2027, проект")
        self.assertEqual(parse_file(university(admission_year=2026), self.c).status_label(2027),
                         "приём 2026 — прошлый год")

    def test_repository_files_are_valid(self):
        """Файлы в benefits/ должны проходить проверку против настоящего каталога."""
        rsr = PROJECT_ROOT / "data" / "rsr"
        if not list(rsr.glob("rsr_*.json")):
            self.skipTest("нет скачанных перечней в data/rsr")
        from app.catalog import Catalog

        load_benefits(PROJECT_ROOT / "benefits", Catalog.load(rsr))


class TextsTest(unittest.TestCase):
    def setUp(self):
        self.c = sample_catalog()
        self.u = parse_file(university(), self.c, "test.json")

    def test_olympiad_view(self):
        fiztech = self.c.search("физтех")[0].olympiad
        rules = [r for r in self.u.rules if r.olympiad is fiztech]
        text = texts.olympiad_benefits(fiztech, [(self.u, rules)], 2027)
        self.assertIn("<b>Тестовый вуз</b> — <i>приём 2027</i>", text)
        self.assertIn("🏅 БВИ — победителям: математика", text)
        self.assertIn("💯 100 баллов ЕГЭ — призёрам: любой профиль", text)
        self.assertIn("ЕГЭ от 75 · диплом за 11 класс", text)
        detail = texts.olympiad_university_benefits(fiztech, self.u, rules, 2027)
        self.assertIn("🏅 БВИ — победителям (математика; 1 ур.)", detail)
        self.assertIn("ЕГЭ от 75: предмет профиля · диплом за 11 класс", detail)
        self.assertIn("направления — по профилю олимпиады", detail)

    def test_summary_counts_programs(self):
        from app.benefits import parse_file

        data = university(rules=[
            {"olympiad": "Физтех", "benefit": "bvi", "diploma": "any", "profiles": ["математика"],
             "programs": ["01.03.01 Математика", "01.03.02 ПМИ"], "confirm_min_score": 75},
            {"olympiad": "Физтех", "benefit": "bvi", "diploma": "any", "profiles": ["математика"], "grades": [10],
             "programs": ["01.03.01 Математика"], "confirm_min_score": 85},
            {"olympiad": "Физтех", "benefit": "score100", "diploma": "winner", "profiles": ["физика"],
             "programs": ["03.03.02 Физика"]},
        ])
        u = parse_file(data, self.c)
        fiztech = u.rules[0].olympiad
        text = texts.olympiad_benefits(fiztech, [(u, u.rules)], 2027)
        self.assertIn("🏅 БВИ — победителям и призёрам: математика (2 программы)", text)
        self.assertIn("💯 100 баллов ЕГЭ — победителям: физика (03.03.02 Физика)", text)
        self.assertIn("ЕГЭ от 75 до 85 — смотря по программе · часть льгот — и за 10 класс", text)

    def test_empty_and_vsosh(self):
        o = self.c.search("ломоносов")[0].olympiad
        self.assertIn("Пока нет данных", texts.olympiad_benefits(o, [], 2027))
        vsosh = self.c.search("всош")[0].olympiad
        text = texts.olympiad_benefits(vsosh, [(self.u, [self.u.rules[2]])], 2027)
        self.assertIn("без условия по ЕГЭ", text)

    def test_university_view(self):
        text = texts.university_benefits(self.u, 2027)
        self.assertIn("🏅 <b>БВИ — победителям</b>\n• олимпиады (1): Физтех (математика)", text)
        self.assertIn("💯 <b>100 баллов ЕГЭ — призёрам</b>\n• олимпиады (1): Физтех", text)
        self.assertIn('href="https://example.edu/olymp"', text)
        wild = parse_file(university(rules=[{"olympiad": "*", "profiles": ["математика"], "levels": [1],
                                             "benefit": "bvi", "diploma": "any"}]), self.c)
        self.assertIn("• любая олимпиада I уровня — профили: математика", texts.university_benefits(wild, 2027))
        old = parse_file(university(admission_year=2026), self.c)
        self.assertIn("это данные прошлого года", texts.university_benefits(old, 2027))


if __name__ == "__main__":
    unittest.main()


class PrizeWarningTest(unittest.TestCase):
    def test_warning_only_for_rsosh_prize_bvi(self):
        from types import SimpleNamespace

        from app.bot.texts import prize_bvi_affected

        rsosh, vsosh = SimpleNamespace(is_vsosh=False), SimpleNamespace(is_vsosh=True)
        rule = lambda o, benefit, diploma: SimpleNamespace(olympiad=o, benefit=benefit, diploma=diploma)
        self.assertTrue(prize_bvi_affected([rule(rsosh, "bvi", "any")]))
        self.assertTrue(prize_bvi_affected([rule(rsosh, "bvi", "prize")]))
        self.assertFalse(prize_bvi_affected([rule(rsosh, "bvi", "winner"), rule(rsosh, "score100", "any")]))
        self.assertFalse(prize_bvi_affected([rule(vsosh, "bvi", "any")]))


class NotInListTest(unittest.TestCase):
    def test_banner(self):
        from types import SimpleNamespace

        from app.bot.texts import list_year, not_in_list_banner

        self.assertEqual(list_year(2027), "2026/27")
        gone = SimpleNamespace(is_vsosh=False, profiles_in=lambda y: {})
        stays = SimpleNamespace(is_vsosh=False, profiles_in=lambda y: {"математика": 1})
        rule = SimpleNamespace(profiles=["физика"])
        self.assertIn("Олимпиады нет в перечне 2026/27", not_in_list_banner(gone, [rule], 2027)[0])
        self.assertIn("Профилей физика нет", not_in_list_banner(stays, [rule], 2027)[0])
        self.assertEqual(not_in_list_banner(stays, [SimpleNamespace(profiles=["математика"])], 2027), [])


class FitTest(unittest.TestCase):
    def test_rule_fits(self):
        from types import SimpleNamespace

        from app.benefits import rule_fits

        r = SimpleNamespace(profiles=["математика"], diploma="winner", grades=[11], levels=[1])
        self.assertTrue(rule_fits(r, "математика", "winner", 11, 1))
        self.assertFalse(rule_fits(r, "физика", "winner", 11, 1))
        self.assertFalse(rule_fits(r, "математика", "prize", 11, 1))
        self.assertFalse(rule_fits(r, "математика", "winner", 10, 1))
        self.assertFalse(rule_fits(r, "математика", "winner", 11, 2))
        any_rule = SimpleNamespace(profiles=None, diploma="any", grades=None, levels=None)
        self.assertTrue(rule_fits(any_rule, "химия", "prize", 9, 3))
