import unittest

from app.parsers.spbu_benefits import parse_levels, parse_spbu_rows

N = None
BVI = "Без вступительных испытаний"


class SpbuTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(parse_levels("I–III"), [1, 2, 3])
        self.assertEqual(parse_levels("II–III"), [2, 3])
        self.assertEqual(parse_levels("II"), [2])

    def test_rows(self):
        rows = [
            ["01.00.00 МАТЕМАТИКА И МЕХАНИКА — БАКАЛАВРИАТ", N, "", "", "", "", "", "", "", "", "", ""],
            ["01.03.01 Математика", "Математика", "очная", "1", "Математика", "85", "I", "—", "—", "Математика",
             "Победитель, призѐр", BVI],
            [N, N, N, "2", "Информатика", "75", "I", "—", "Информатика", "Информатика", "Победитель", BVI],
            [N, N, N, N, N, N, "I", "—", "Информатика", "Информатика", "Призѐр", "100 баллов за ВИ по информатике"],
            [N, N, N, "3", "Русский язык", "—", "Особые права не предоставляются", N, N, N, N, N],
            ["39.03.01 Социология", "Социология", "очно-заочная", "1", "История", "75", "I–II", "—", "—",
             "Социология и социальная", "Победитель, призёр", BVI],
            [N, N, N, N, N, N, "I", 'Олимпиада школьников "Ломоносов"', "Философия", "Обществознание",
             "Победитель", BVI],
        ]
        result = parse_spbu_rows(rows)
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.rows, 5)
        math, inf_bvi, inf_100, soc, lomonosov = result.rules
        self.assertEqual((math["olympiad"], math["subjects"], math["confirm_min_score"]), ("*", ["математика"], 85))
        self.assertEqual(math["programs"], ["01.03.01 Математика — Математика"])
        self.assertEqual((inf_bvi["profiles"], inf_bvi["diploma"]), (["информатика"], "winner"))
        self.assertEqual((inf_100["benefit"], inf_100["score_for"]), ("score100", "ВИ по информатике"))
        self.assertEqual(soc["subjects"], ["социология и социальная работа"])
        self.assertEqual(soc["levels"], [1, 2])
        self.assertEqual(soc["confirm_subject"], "история")
        self.assertEqual(soc["programs"], ["39.03.01 Социология — Социология (очно-заочная)"])
        self.assertEqual(lomonosov["olympiad"], 'Олимпиада школьников "Ломоносов"')


if __name__ == "__main__":
    unittest.main()
