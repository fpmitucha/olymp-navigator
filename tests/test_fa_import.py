import unittest

from app.parsers.fa_benefits import clean_name, parse_fa_rows

N = None


class FaTest(unittest.TestCase):
    def test_clean_name(self):
        self.assertEqual(clean_name("Всероссийская о4л импиада школьников «Высшая проба»"),
                         "Всероссийская олимпиада школьников «Высшая проба»")
        self.assertEqual(clean_name("«2Н аследники Левши»"), "«Наследники Левши»")
        self.assertEqual(clean_name("Всероссийская Т6 олстовская олимпиада"), "Всероссийская Толстовская олимпиада")
        self.assertEqual(clean_name("«Формула Единства» / 3 «Третье тысячелетие»"),
                         "«Формула Единства» / «Третье тысячелетие»")

    def test_rows(self):
        rows = [
            ["1", "«Финатлон» - олимпиада по", "финансовая грамотность", "обществознание", "Группа 4", "+", "+",
             "3", "3", "3", "3", "-"],
            [N, N, N, "математика", "Группа 3", "+", "+", "3", "3", "3", "3", "-"],
            ["", "финансовой грамотности", "", "", "", "", "", "", "", "", "", ""],
            ["2", "Олимпиада", "русский язык", "русский язык", "Все группы", "-", "+", "-", "2", "-", "-", "-"],
        ]
        result = parse_fa_rows(rows)
        self.assertEqual(len(result.rules), 5)
        first = result.rules[0]
        self.assertEqual(first["olympiad"], "«Финатлон» - олимпиада по финансовой грамотности")
        self.assertEqual((first["benefit"], first["programs"]), ("bvi", ["группа программ 4"]))
        self.assertIn("2025/26 — 3", first["note"])
        last = result.rules[-1]
        self.assertEqual((last["benefit"], last["programs"], last["note"]),
                         ("score100", None, "уровень по годам диплома: 2024/25 — 2"))


if __name__ == "__main__":
    unittest.main()
