import unittest

from app.parsers.sechenov_benefits import lines_from_words, parse_lines


def w(text, x, top):
    return {"text": text, "x0": x, "top": top}


class SechenovTest(unittest.TestCase):
    def test_wrapped_cells(self):
        words = [
            w("№", 63, 10), w("Полное", 137, 10), w("Профиль", 300, 10),
            w("(не", 590, 20), w("менее", 610, 20), w("75", 640, 20), w("баллов)", 650, 20),
            w("1.", 72, 40), w("Олимпиада", 137, 40), w("СПбГУ", 200, 40),
            w("математика", 300, 40), w("математика", 490, 40), w("математика", 588, 40),
            w("право", 686, 40), w("на", 715, 40), w("100", 730, 40), w("баллов", 750, 40),
            w("школьников", 137, 52), w("моделирование", 300, 52),
            w("информатика", 490, 64), w("информатика", 588, 64), w("право", 686, 64),
        ]
        result = parse_lines(lines_from_words(words))
        self.assertEqual(result.rows, 2)
        first, second = result.rules
        self.assertEqual(first["olympiad"], "Олимпиада СПбГУ школьников")
        self.assertEqual(first["profiles"], ["математика моделирование"])
        self.assertEqual(second["profiles"], ["математика моделирование"])
        self.assertEqual(second["score_for"], "ЕГЭ по предмету «информатика»")
        self.assertEqual(second["confirm_subject"], "информатика")


if __name__ == "__main__":
    unittest.main()
