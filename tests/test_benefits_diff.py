import unittest

from app.benefits import parse_file
from app.benefits_diff import diff_universities, summarize
from tests.helpers import sample_catalog
from tests.test_benefits import university


class DiffTest(unittest.TestCase):
    def setUp(self):
        self.c = sample_catalog()

    def test_changes(self):
        old = parse_file(university(admission_year=2026, rules=[
            {"olympiad": "Физтех", "benefit": "bvi", "diploma": "any", "profiles": ["математика"]},
            {"olympiad": "Физтех", "benefit": "score100", "diploma": "any", "profiles": ["физика"],
             "confirm_min_score": 75},
            {"olympiad": "ВсОШ", "benefit": "bvi", "diploma": "any", "confirm_min_score": None, "grades": None},
        ]), self.c)
        new = parse_file(university(rules=[
            # победителям БВИ остался, призёрам — только 100 баллов (как в проекте Минобрнауки)
            {"olympiad": "Физтех", "benefit": "bvi", "diploma": "winner", "profiles": ["математика"]},
            {"olympiad": "Физтех", "benefit": "score100", "diploma": "prize", "profiles": ["математика"]},
            {"olympiad": "Физтех", "benefit": "score100", "diploma": "any", "profiles": ["физика"],
             "confirm_min_score": 85},
        ]), self.c)
        changes = {(c.kind, c.profile, c.diploma) for c in diff_universities(old, new)}
        self.assertIn(("bvi_to_100", "математика", "prize"), changes)
        self.assertNotIn(("removed", "математика", "winner"), changes)  # разделение any → winner не «удаление»
        self.assertIn(("stricter", "физика", "winner"), changes)
        self.assertIn(("removed", "*", "winner"), changes)  # ВсОШ убрали
        text = "\n".join(summarize(diff_universities(old, new)))
        self.assertIn("БВИ больше нет", text)
        self.assertIn("ЕГЭ от 75 → от 85", text)

    def test_no_changes(self):
        u = parse_file(university(), self.c)
        self.assertEqual(diff_universities(u, u), [])


class NotifyTest(unittest.TestCase):
    def test_subscribers_get_changes_once(self):
        import asyncio

        from app.benefits import BenefitsCatalog
        from app.bot.notifier import send_benefits_updates
        from app.bot.storage import Storage

        c = sample_catalog()
        storage = Storage(":memory:")
        self.addCleanup(storage.close)
        fiztech = c.search("Физтех")[0].olympiad
        storage.subscribe_university(7, "Тестовый вуз")
        storage.subscribe(7, fiztech.id)
        sent: list[tuple[int, str]] = []

        async def send(telegram_id, text):
            sent.append((telegram_id, text))

        def run(u):
            return asyncio.run(send_benefits_updates(storage, c, BenefitsCatalog([u]), send, pause=0))

        old = parse_file(university(admission_year=2026), c)
        self.assertEqual(run(old), 0)  # первый запуск — только запомнили
        new = parse_file(university(rules=[
            {"olympiad": "Физтех", "benefit": "score100", "diploma": "any", "profiles": ["математика"]}]), c)
        self.assertEqual(run(new), 1)
        self.assertIn("льготы на приём 2027", sent[0][1])
        self.assertIn("Сравнение с приёмом 2026", sent[0][1])
        self.assertIn("⭐", sent[0][1])  # Физтех — в подписках
        self.assertEqual(run(new), 0)  # второй раз о том же не пишем


if __name__ == "__main__":
    unittest.main()
