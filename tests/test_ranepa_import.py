import unittest

from app.parsers.ranepa_benefits import parse_100_rows, parse_bvi_xlsx, parse_vsosh_rows
from app.parsers.xlsx import write_xlsx

N = None


class RanepaTest(unittest.TestCase):
    def test_bvi_sheet(self):
        header = {"A": "Образовательная программа", "B": "Код", "C": "Предмет", "D": "Олимпиада", "E": "Профиль",
                  "F": "Уровень образования"}
        row = lambda prog, conf, prof: {"A": prog, "B": "38.03.01 Экономика", "C": conf,
                                        "D": '"Финатлон"', "E": prof, "F": "Бакалавриат"}
        data = write_xlsx([header, row("Финансы", "Математика", "финансовая грамотность"),
                           row("Финансы", "Обществознание", "финансовая грамотность"),
                           row("Банки", "Математика", "финансовая грамотность; экономика")])
        # лист 3 — тот же формат: тестовый xlsx содержит только sheet1, подменяем номер листа
        import app.parsers.ranepa_benefits as mod
        original = mod.read_xlsx
        mod.read_xlsx = lambda d, sheet=1: original(d, sheet=1)
        try:
            result = parse_bvi_xlsx(data)
        finally:
            mod.read_xlsx = original
        by_key = {(r["profiles"][0], r.get("confirm_subject")): r for r in result.rules}
        self.assertEqual(by_key[("финансовая грамотность", "математика или обществознание")]["programs"],
                         ["38.03.01 Экономика — Финансы"])
        self.assertEqual(by_key[("финансовая грамотность", "математика")]["programs"], ["38.03.01 Экономика — Банки"])
        self.assertIn(("экономика", "математика"), by_key)
        self.assertEqual(result.rules[0]["olympiad"], "«Финатлон»")

    def test_100_and_vsosh(self):
        rows = [["1", "Олимпиада РАНХиГС", "экономика", "математика"], [N, N, N, "обществознание"],
                [N, N, "история", "история"]]
        rules = parse_100_rows(rows).rules
        self.assertEqual(rules[0]["score_for"], "ЕГЭ по предмету «математика» или «обществознание»")
        self.assertEqual(rules[0]["grades"], [11])
        self.assertNotIn("confirm_subject", rules[1])
        vs = parse_vsosh_rows([["1", "Английский язык", "БАКАЛАВРИАТ 01.03.02 ПМИ 09.03.03 ПИ СПЕЦИАЛИТЕТ 37.05.02 Псих"],
                               ["2", "Немецкий язык", N]], ["английский язык", "немецкий язык"])
        self.assertEqual(vs.rules[1]["programs"], ["01.03.02 ПМИ", "09.03.03 ПИ", "37.05.02 Псих"])


if __name__ == "__main__":
    unittest.main()
