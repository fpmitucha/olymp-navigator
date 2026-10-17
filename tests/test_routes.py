import unittest
from types import SimpleNamespace

from app.routes import covers, program_label


def rule(programs):
    return SimpleNamespace(programs=programs)


class CoversTest(unittest.TestCase):
    def test_no_programs_means_all(self):
        self.assertTrue(covers(rule(None), "01.03.02"))

    def test_exact(self):
        self.assertTrue(covers(rule(["01.03.02", "09.03.01"]), "09.03.01"))
        self.assertFalse(covers(rule(["01.03.02"]), "09.03.01"))

    def test_all_except(self):
        r = rule(["все, кроме 41.03.05"])
        self.assertTrue(covers(r, "09.03.01"))
        self.assertFalse(covers(r, "41.03.05"))

    def test_label(self):
        self.assertEqual(program_label("38.03.01", {"38.03.01": "Экономика"}), "38.03.01 Экономика")
        self.assertEqual(program_label("Экономика", {}), "Экономика")


if __name__ == "__main__":
    unittest.main()
