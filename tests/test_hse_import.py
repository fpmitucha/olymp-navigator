"""Импорт таблицы особых прав ВШЭ. Таблица собрана из строк настоящего приложения 2 (приём 2026)."""

from __future__ import annotations

import unittest

from app.benefits import parse_file, resolve_import
from app.parsers.hse_benefits import parse_hse_table
from app.parsers.xlsx import read_xlsx, write_xlsx
from tests.helpers import sample_catalog

HEADER = {"A": "№ п/п", "B": "Наименование образовательной программы",
          "C": "Полное наименование олимпиады из Перечня олимпиад школьников***", "D": "Профиль олимпиады",
          "E": "Предмет олимпиады", "F": "Предметы ЕГЭ", "G": "Количество баллов ЕГЭ", "H": "Вид особого права",
          "I": "Предмет зачета 100 баллов", "J": "Кому предоставляется", "K": "В каких классах"}


def row(**cells):
    return {k: v for k, v in cells.items()}


TABLE = [
    row(H="Приложение 2"),
    row(A="Особые права при приеме на обучение по программам бакалавриата НИУ ВШЭ (г. Москва) для поступающих в 2026 году"),
    HEADER,
    row(A="Направление подготовки 01.03.01 Математика"),
    row(A="1", B="Математика", C="Олимпиада школьников «Физтех»", D="математика", E="математика", F="математика",
        G="75 и более", H="Право на прием БВИ", I="математика", J="Победителям и призерам", K="11 класс"),
    row(D="математика", E="математика", F="математика", G="75 и более", H="Право на прием БВИ", I="математика",
        J="Победителям и призерам", K="10 класс"),
    row(D="физика", E="физика", F="физика", G="75 и более", H="Право на прием БВИ", I="физика", J="Победителям ",
        K="11 класс"),
    # строка 100 баллов под строкой БВИ: профиль и балл подразумеваются из строки выше
    row(H="Право на 100 баллов", I="физика", J="Победителям и Призерам", K=" 11 класс"),
    row(C="Все олимпиады, входящие в Перечень олимпиад школьников", D="история", F="история", G="75 и более",
        H="Право на 100 баллов", J="Победителям и призерам", K="11 класс"),
    row(A="Направление подготовки 01.03.02 Прикладная математика и информатика"),
    row(A="3", B="Прикладная математика и информатика", C="Олимпиада школьников “Физтех”", D="математика",
        F="математика", G="85 и более", H="Право на прием БВИ", J="Победителям и призерам", K="10, 11 класс"),
    row(C="Неизвестная олимпиада", D="математика", F="математика", G="75 и более", H="Право на прием БВИ",
        J="Победителям", K="11 класс"),
    row(C="Олимпиада школьников «Физтех»", D="Астрология", F="математика", G="75 и более",
        H="Право на 100 баллов", J="Победителям", K="11 класс"),
    row(A="Направление подготовки 42.03.02 Журналистика"),
    row(A="9", B="Журналистика", C="Олимпиада школьников «Физтех»", D="математика", F="", G="-",
        H="Право на 100 баллов", J="Победителям и призерам", K="11 класс"),
]


class XlsxTest(unittest.TestCase):
    def test_roundtrip(self):
        rows = [{"A": "x", "C": "<y> & z"}, {"B": "Математика"}]
        self.assertEqual(read_xlsx(write_xlsx(rows)), rows)


class HseImportTest(unittest.TestCase):
    def setUp(self):
        self.table = parse_hse_table(write_xlsx(TABLE))

    def rules(self, **match):
        return [r for r in self.table.rules if all(r.get(k) == v for k, v in match.items())]

    def test_title_and_counts(self):
        self.assertIn("для поступающих в 2026 году", self.table.title)
        self.assertEqual(self.table.skipped_generic, 1)
        self.assertEqual(self.table.programs, 3)

    def test_fill_down_and_normalization(self):
        bvi_phys = self.rules(profiles=["физика"], benefit="bvi")
        self.assertEqual(len(bvi_phys), 1)
        self.assertEqual((bvi_phys[0]["diploma"], bvi_phys[0]["confirm_min_score"]), ("winner", 75))
        score_phys = self.rules(profiles=["физика"], benefit="score100")[0]
        self.assertEqual((score_phys["diploma"], score_phys["confirm_min_score"], score_phys["grades"]),
                         ("any", 75, [11]))  # профиль и балл — из строки выше
        self.assertEqual(score_phys["programs"], ["01.03.01 Математика"])

    def test_grades_and_programs(self):
        tenth = self.rules(profiles=["математика"], benefit="bvi", grades=[10])
        self.assertEqual(tenth[0]["programs"], ["01.03.01 Математика"])
        pmi = self.rules(profiles=["математика"], grades=[10, 11])[0]
        self.assertEqual((pmi["confirm_min_score"], pmi["programs"]),
                         (85, ["01.03.02 Прикладная математика и информатика"]))

    def test_creative_program_without_score(self):
        journalism = self.rules(programs=["42.03.02 Журналистика"])[0]
        self.assertIsNone(journalism["confirm_min_score"])

    def test_resolve_against_catalog(self):
        catalog = sample_catalog()
        kept, problems = resolve_import(self.table.rules, catalog)
        self.assertEqual(len(kept), len(self.table.rules) - 2)
        self.assertTrue(any("Неизвестная олимпиада" in p for p in problems))
        self.assertTrue(any("астрология" in p for p in problems))
        doc = {"university": "ВШЭ", "admission_year": 2026, "document_status": "approved",
               "source_url": "https://www.hse.ru/docs/1130197982.html", "checked_at": "2026-10-15", "rules": kept}
        u = parse_file(doc, catalog, "hse.json")
        self.assertEqual(len({r.olympiad.id for r in u.rules}), 1)  # «Физтех» и “Физтех” — одна олимпиада


if __name__ == "__main__":
    unittest.main()
