import unittest

from app.parsers.itmo_benefits import Joiner, parse_rsosh_rows, parse_vsosh_rows

N = None
JOIN = Joiner({"информатика", "природопользование", "игр", "обществознание"})


class ItmoTest(unittest.TestCase):
    def test_joiner(self):
        self.assertEqual(JOIN("Информат\nика"), "Информатика")
        self.assertEqual(JOIN("05.03.06 Экология\nи\nприродопользован\nие"), "05.03.06 Экология и природопользование")
        self.assertEqual(JOIN("компьютерных\nигр"), "компьютерных игр")
        self.assertEqual(JOIN("Бизнес-\nинформатика"), "Бизнес-информатика")
        self.assertEqual(JOIN("математика и\nинформатика"), "математика и информатика")

    def test_rsosh_rows(self):
        rows = [
            ["еинелварпаН", "ыдаипмило", "ьлифорП", "...", "ыдаипмило ьневорУ", "молпиД"],
            ["01.03.02 Прикладная математика, 09.03.02 Системы", "Все из перечня олимпиад школьников",
             "Информатика (только на\nнаправление 01.03.02)", "Информат\nика", "1, 2 или 3", "Победитель или призер"],
            [N, "Олимпиада школьников «Ломоносов»", "Иностранный язык/ Английский язык", "Обществоз\nнание", "1 или 2",
             "Победитель"],
        ]
        result = parse_rsosh_rows(rows, "score100", JOIN)
        any_rule, lomonosov = result.rules
        self.assertEqual(any_rule["olympiad"], "*")
        self.assertEqual(any_rule["profiles"], ["информатика"])
        self.assertEqual(any_rule["note"], "только на направление 01.03.02")
        self.assertEqual(any_rule["programs"], ["01.03.02 Прикладная математика", "09.03.02 Системы"])
        self.assertEqual(any_rule["levels"], [1, 2, 3])
        self.assertEqual(lomonosov["profiles"], ["иностранный язык", "английский язык"])
        self.assertEqual((lomonosov["diploma"], lomonosov["confirm_subject"]), ("winner", "обществознание"))
        self.assertEqual(lomonosov["programs"], any_rule["programs"])

    def test_vsosh_rows(self):
        rows = [["01.03.02 ПМИ\n10.03.01 ИБ", "Технология\n(Робототехника\nи(или)\nИнформационная\nбезопасность)"],
                ["38.03.05 Бизнес-информатика", "Обществознание,\nПраво"]]
        result = parse_vsosh_rows(rows, JOIN, ["обществознание", "право", "труд (технология)"])
        self.assertEqual(result.warnings, [])
        self.assertEqual([r["profiles"] for r in result.rules], [["труд (технология)"], ["обществознание", "право"]])
        self.assertIn("Робототехника", result.rules[0]["note"])


if __name__ == "__main__":
    unittest.main()
