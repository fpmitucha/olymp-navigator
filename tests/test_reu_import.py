import unittest

from app.parsers.reu_benefits import parse_reu_rows

N = None


class ReuTest(unittest.TestCase):
    def test_rows(self):
        rows = [
            ["1", "Прикладная математика и информатика"], ["10", "Экономика"], ["11", "Менеджмент"],
            ["2025/2026 год", N, N, N, N, N, N, N],
            ["№ п/п", "№", "Название", "Предметы", "Профиль", "Тип льготы", "Направление", "Подтверждающий предмет ЕГЭ"],
            ["2", "2", "«Формула Единства»", "математика", "математика", "БВИ", "1,10", "математика"],
            [N, N, N, N, N, "100 баллов", "1,10,11", "математика"],
            ["", "", "", "экономика", "экономика", "БВИ*", "10,11", "обществознание"],
            ["76", "76", "Всероссийская олимпиада школьников (заключительный этап)", "право", "право", "100 баллов*",
             "все направления", "обществознание"],
            ["2024/2025 год", N, N, N, N, N, N, N],
            ["2", "2", "«Формула Единства»", "математика", "математика", "БВИ", "1", "математика"],
        ]
        result = parse_reu_rows(rows)
        self.assertEqual((result.year, result.rows), ("2025/2026", 4))
        bvi, score, econ, vsosh = result.rules
        self.assertEqual(bvi["programs"], ["Прикладная математика и информатика", "Экономика"])
        self.assertEqual(score["score_for"], "ЕГЭ по предмету «математика»")
        self.assertEqual((econ["olympiad"], econ["profiles"], econ["confirm_subject"]),
                         ("«Формула Единства»", ["экономика"], "обществознание"))
        self.assertEqual((vsosh["olympiad"], vsosh["programs"], vsosh["confirm_min_score"]), ("ВсОШ", None, None))


if __name__ == "__main__":
    unittest.main()
