"""Тесты парсера olimpiada.ru на фрагментах настоящих страниц (15.10.2026)."""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from app.parsers.olimpiada import classify_event, parse_activity, parse_sitemap, parse_when

FIXTURES = Path(__file__).parent / "fixtures" / "olimpiada"
TODAY = date(2026, 10, 15)


def page(activity_id: int):
    html = (FIXTURES / f"activity_{activity_id}.html").read_text(encoding="utf-8")
    return parse_activity(html, activity_id, TODAY)


class WhenTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_when("20\xa0авг...22\xa0сен", 2026), (date(2026, 8, 20), date(2026, 9, 22)))
        self.assertEqual(parse_when("29 сен", 2026), (date(2026, 9, 29), date(2026, 9, 29)))
        self.assertEqual(parse_when("До\xa031\xa0окт", 2026), (None, date(2026, 10, 31)))
        self.assertEqual(parse_when("Уточняется", 2026), (None, None))
        self.assertEqual(parse_when("", 2026), (None, None))

    def test_academic_year_rule(self):
        self.assertEqual(parse_when("13 янв...7 фев", 2026), (date(2027, 1, 13), date(2027, 2, 7)))
        self.assertEqual(parse_when("20 дек...10 янв", 2026), (date(2026, 12, 20), date(2027, 1, 10)))
        self.assertEqual(parse_when("14 мар", 2026), (date(2027, 3, 14), date(2027, 3, 14)))
        self.assertEqual(parse_when("15 июл...20 авг", 2026), (date(2026, 7, 15), date(2026, 8, 20)))

    def test_full_month_names_and_same_month_range(self):
        self.assertEqual(parse_when("5 ноября", 2026), (date(2026, 11, 5), date(2026, 11, 5)))
        self.assertEqual(parse_when("1 мая", 2026), (date(2027, 5, 1), date(2027, 5, 1)))
        self.assertEqual(parse_when("20–22 ноя", 2026), (date(2026, 11, 20), date(2026, 11, 22)))
        self.assertEqual(parse_when("31 фев", 2026), (None, None))

    def test_classify(self):
        self.assertEqual(classify_event("Регистрация на отборочный этап"), "registration")
        self.assertEqual(classify_event("Финал"), "final")
        self.assertEqual(classify_event("Второй день финала для 11 классов"), "final")
        self.assertEqual(classify_event("Заключительный этап"), "final")
        self.assertEqual(classify_event("Первый тур отборочного этапа: 1 день"), "qualifying")
        self.assertEqual(classify_event("Публикация результатов"), "results")
        self.assertEqual(classify_event("Вебинар для учителей"), "other")


class ActivityTest(unittest.TestCase):
    def test_fiztech_math(self):
        a = page(395)
        self.assertEqual(a.name, "Олимпиада школьников «Физтех» по математике")
        self.assertEqual(a.parent_name, "Олимпиада школьников «Физтех»")
        self.assertEqual(a.subjects, ["Математика"])
        self.assertEqual((a.rsr_year, a.rsr_number, a.rsr_levels), ("2026/27", 38, "уровень 1"))
        self.assertEqual(len(a.events), 7)
        self.assertEqual([e.past for e in a.events[:3]], [True, True, False])
        reg = a.events[2]
        self.assertEqual((reg.event_id, reg.name, reg.kind), (23148, "Регистрация на второй тур", "registration"))
        self.assertEqual((reg.starts_at, reg.ends_at), (None, date(2026, 10, 31)))
        final = a.events[-1]
        self.assertEqual((final.kind, final.starts_at), ("final", date(2027, 2, 14)))
        self.assertEqual(a.event_url(final), "https://olimpiada.ru/activity/395/events/23152")

    def test_nto_levels_and_unknown_date(self):
        a = page(5369)
        self.assertEqual(a.rsr_number, 3)
        self.assertEqual(a.rsr_levels, "уровни II и III по 23 направлениям")
        self.assertEqual(len(a.subjects), 9)
        last = a.events[-1]
        self.assertEqual((last.name, last.starts_at, last.ends_at), ("Заключительный этап", None, None))
        self.assertEqual(a.events[2].ends_at, date(2026, 12, 11))

    def test_new_year_crossing(self):
        a = page(5755)
        final = a.events[-1]
        self.assertEqual((final.starts_at, final.ends_at), (date(2027, 1, 13), date(2027, 2, 7)))

    def test_no_schedule(self):
        a = page(5881)
        self.assertEqual(a.rsr_number, 52)
        self.assertEqual(a.events, [])

    def test_spring_events(self):
        a = page(1)
        self.assertEqual(a.rsr_number, 26)
        self.assertEqual([e.starts_at for e in a.events], [date(2027, 3, 14), date(2027, 3, 14), date(2027, 3, 27)])


class ListTest(unittest.TestCase):
    def test_sitemap(self):
        xml = (FIXTURES / "sitemap_sample.xml").read_text(encoding="utf-8")
        self.assertEqual(parse_sitemap(xml), [1, 395])  # без событий, новостей и повторов

    def test_parent_page_lists_children(self):
        a = page(201)
        self.assertEqual(a.name, "Физико-математическая олимпиада «Росатом»")
        self.assertEqual(a.child_ids, [5881, 411])  # ссылки ?class=7 не считаются
        self.assertIsNone(a.parent_id)
        self.assertIsNone(a.rsr_number)

    def test_child_knows_parent(self):
        self.assertEqual(page(395).parent_id, 123)
        self.assertEqual(page(5881).parent_id, 201)
        self.assertEqual(page(395).child_ids, [])


if __name__ == "__main__":
    unittest.main()
