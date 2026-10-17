import unittest

from app.parsers.msu_benefits import (
    parse_benefits, parse_diploma, parse_levels, parse_msu_rows, split_olympiads,
)
from tests.test_benefits import sample_catalog

N = None
HEADER = ["Направление подготовки", "Профиль олимпиады", "Предметы", "Перечень олимпиад", "Уровень олимпиад",
          "Класс", "Победитель/ призер", "Предмет ЕГЭ не менее 75", "Предоставляемая льгота"]
BVI = "Зачисление без вступительных испытаний"
DVI = "Максимальное количество баллов по дополнительному вступительному испытанию по математике"
EGE = "Максимальное количество баллов по ЕГЭ по физике"


class PartsTest(unittest.TestCase):
    def test_split(self):
        self.assertEqual(
            split_olympiads("Московская олимпиада школьников, Олимпиада «Физтех, и не только»,Турнир городов; НТО"),
            ["Московская олимпиада школьников", "Олимпиада «Физтех, и не только»", "Турнир городов", "НТО"])

    def test_cells(self):
        self.assertEqual(parse_levels("I, II"), [1, 2])
        self.assertEqual(parse_levels("I,II, III"), [1, 2, 3])
        self.assertEqual(parse_diploma("Победитель, призёр"), "any")
        self.assertEqual(parse_diploma("Призер"), "prize")
        self.assertEqual(parse_benefits(DVI + "; Максимальное количество баллов ЕГЭ по математике"),
                         [("dvi100", "ДВИ по математике"), ("score100", "ЕГЭ по математике")])
        self.assertEqual(parse_benefits(BVI), [("bvi", None)])


class RowsTest(unittest.TestCase):
    def test_merged_cells_and_faculties(self):
        pages = [[
            ["МЕХАНИКО-МАТЕМАТИЧЕСКИЙ ФАКУЛЬТЕТ", "", "", "", "", "", "", "", ""],
            HEADER,
            ["Математика", "Математика", "Математика", "Олимпиада МФТИ «Физтех»", "I", "11", "Победитель", "Математика", BVI],
            [N, N, N, N, N, N, "Призер", N, DVI],
            [N, "Физика", "Физика", "*", "I, II", "11", "Победитель, призер", "Физика", EGE],
        ], [
            ["", N, N, N, N, N, N, N, N],  # продолжение на новой странице
            ["ФИЗИЧЕСКИЙ ФАКУЛЬТЕТ", "", "", "", "", "", "", "", ""],
            ["Физика", "Математика", "Математика", "Олимпиада МФТИ «Физтех»", "I", "11", "Победитель", "Математика", BVI],
        ]]
        result = parse_msu_rows(pages)
        self.assertEqual(result.faculties, 2)
        self.assertEqual(result.warnings, [])
        bvi = [r for r in result.rules if r["benefit"] == "bvi"]
        self.assertEqual(len(bvi), 1)
        self.assertEqual(bvi[0]["programs"], ["Механико-математический факультет: Математика", "Физический факультет: Физика"])
        dvi = next(r for r in result.rules if r["benefit"] == "dvi100")
        self.assertEqual((dvi["diploma"], dvi["score_for"], dvi["levels"]), ("prize", "ДВИ по математике", [1]))
        wildcard = next(r for r in result.rules if r["olympiad"] == "*")
        self.assertEqual((wildcard["profiles"], wildcard["levels"], wildcard["benefit"]), (["физика"], [1, 2], "score100"))

    def test_wildcard_rules_expand_on_load(self):
        from app.benefits import parse_file, resolve_import

        catalog = sample_catalog()
        rules = [{"olympiad": "*", "profiles": ["математика"], "benefit": "score100", "programs": ["Мехмат"]},
                 {"olympiad": "*", "subjects": ["Физика"], "levels": [1], "benefit": "bvi"},
                 {"olympiad": "*", "profiles": ["нет такого"], "levels": [1], "benefit": "score100"}]
        kept, problems = resolve_import(rules, catalog)
        self.assertEqual(len(kept), 2)
        self.assertEqual(len(problems), 1)
        doc = {"university": "Т", "admission_year": 2026, "document_status": "approved",
               "source_url": "https://x", "checked_at": "2026-10-15", "rules": kept}
        u = parse_file(doc, catalog, "t.json")
        self.assertTrue(u.rules)
        self.assertTrue(all(r.wildcard and r.olympiad for r in u.rules))
        self.assertIn("по профилю «математика»", u.rules[0].wildcard)


if __name__ == "__main__":
    unittest.main()
