import unittest

from app.parsers.msu_benefits import (
    expand_wildcards, parse_benefits, parse_diploma, parse_levels, parse_msu_rows, split_olympiads,
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

    def test_expand_wildcards(self):
        catalog = sample_catalog()
        year = catalog.current_year
        rules = [{"olympiad": "*", "profiles": ["математика"], "levels": [1, 2, 3], "benefit": "score100"},
                 {"olympiad": "*", "profiles": ["нет такого"], "levels": [1], "benefit": "score100"}]
        out, problems = expand_wildcards(rules, catalog, year)
        self.assertTrue(out)
        self.assertTrue(all(r["olympiad"] != "*" and r["profiles"] for r in out))
        self.assertEqual(len(problems), 1)


if __name__ == "__main__":
    unittest.main()
