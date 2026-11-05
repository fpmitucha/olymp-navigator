import unittest

from app.parsers.sechenov_benefits import split_subjects


class SechenovTest(unittest.TestCase):
    def test_split_subjects(self):
        self.assertEqual(split_subjects("информатика математика"), ["информатика", "математика"])
        self.assertEqual(split_subjects("русский язык иностранный язык"), ["русский язык", "иностранный язык"])
        self.assertEqual(split_subjects("право"), [])


if __name__ == "__main__":
    unittest.main()
