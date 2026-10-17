import unittest

from app.parsers.mephi_benefits import parse_mephi_html, table_grid
from bs4 import BeautifulSoup

HTML = """<p>Особые права победителей и призеров олимпиад школьников, поступающих в НИЯУ МИФИ в 2026 году</p>
<table><tr><td>№ в Перечне олимпиад</td><td>Полное наименование олимпиады</td><td>Уровень олимпиады</td>
<td>Поступление БВИ</td><td>Засчитывание 100 баллов</td><td>Общеобразовательный предмет</td><td>Профиль олимпиады</td></tr>
<tr><td rowspan="3">54</td><td rowspan="3">Олимпиада школьников «Физтех»</td><td>II</td><td>все, кроме 41.03.05</td><td></td>
<td>математика</td><td>математика</td></tr>
<tr><td>I</td><td>01.03.02, 09.03.01</td><td></td><td>физика</td><td>физика</td></tr>
<tr><td>III</td><td></td><td>01.03.02 / все, кроме 41.03.05</td><td>информатика/математика</td><td>Анализ данных</td></tr>
<tr><td>69</td><td>Газпром</td><td>III</td><td></td><td>все</td><td>физика</td><td></td></tr>
</table>"""


class MephiTest(unittest.TestCase):
    def test_grid_rowspan(self):
        grid = table_grid(BeautifulSoup(HTML, "lxml").find("table"))
        self.assertEqual(grid[3][:3], ["54", "Олимпиада школьников «Физтех»", "III"])

    def test_rules(self):
        result = parse_mephi_html(HTML)
        self.assertIn("2026 году", result.title)
        self.assertEqual(result.rows, 3)
        self.assertEqual(len(result.warnings), 1)  # строка без профиля
        math, phys, data = result.rules
        self.assertEqual((math["benefit"], math["levels"], math["programs"]), ("bvi", [2], ["все, кроме 41.03.05"]))
        self.assertEqual(phys["programs"], ["01.03.02", "09.03.01"])
        self.assertEqual(phys["grades"], [10, 11])
        self.assertEqual(data["benefit"], "score100")
        self.assertEqual(data["grades"], [8, 9, 10, 11])
        self.assertEqual(data["profiles"], ["анализ данных"])
        self.assertEqual(data["confirm_subject"], "информатика или математика")
        self.assertEqual(data["programs"], ["01.03.02", "все, кроме 41.03.05 (по предмету «математика»)"])


if __name__ == "__main__":
    unittest.main()
