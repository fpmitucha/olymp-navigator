from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.stages import Stage, due_reminders, read_stages_csv, reminder_kind

TODAY = date(2026, 11, 10)


def stage(id=1, kind="qualifying", start=None, end=None, olympiad_id="o1"):
    return Stage(id=id, olympiad_id=olympiad_id, kind=kind, title="Отборочный этап", starts_at=start, ends_at=end)


class ReminderKindTest(unittest.TestCase):
    def test_stage_windows(self):
        cases = {10: None, 8: None, 7: "stage_t7", 3: "stage_t7", 2: "stage_t7", 1: "stage_t1", 0: "stage_day"}
        for days, expected in cases.items():
            s = stage(start=date.fromordinal(TODAY.toordinal() + days))
            got = reminder_kind(s, TODAY)
            self.assertEqual(got[0] if got else None, expected, days)
            if got:
                self.assertEqual(got[1], days)

    def test_past_stage(self):
        self.assertIsNone(reminder_kind(stage(start=date(2026, 11, 9)), TODAY))

    def test_registration_uses_deadline(self):
        s = stage(kind="registration", start=date(2026, 10, 1), end=date(2026, 11, 13))
        self.assertEqual(reminder_kind(s, TODAY), ("deadline_t3", 3))
        s = stage(kind="registration", start=date(2026, 10, 1), end=TODAY)
        self.assertEqual(reminder_kind(s, TODAY), ("deadline_day", 0))
        s = stage(kind="registration", start=date(2026, 10, 1), end=date(2026, 11, 20))
        self.assertIsNone(reminder_kind(s, TODAY))

    def test_no_dates(self):
        self.assertIsNone(reminder_kind(stage(), TODAY))


class DueRemindersTest(unittest.TestCase):
    def test_sent_are_skipped_and_only_subscribers_get(self):
        s1 = stage(id=1, start=date(2026, 11, 11))
        s2 = stage(id=2, start=date(2026, 11, 15), olympiad_id="o2")
        subs = {"o1": [100, 200], "o2": [100]}
        sent = {(100, 1, "stage_t1")}
        due = due_reminders(TODAY, [s1, s2], subs, sent)
        self.assertEqual(sorted((r.telegram_id, r.stage.id, r.kind) for r in due),
                         [(100, 2, "stage_t7"), (200, 1, "stage_t1")])

    def test_t7_already_sent_then_t1_arrives(self):
        s = stage(id=1, start=date(2026, 11, 11))
        due = due_reminders(TODAY, [s], {"o1": [1]}, {(1, 1, "stage_t7")})
        self.assertEqual([r.kind for r in due], ["stage_t1"])


class CsvImportTest(unittest.TestCase):
    def write(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig")
        tmp.write(text)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def resolve(self, text):
        return {"Физтех": "f1", "НТО": "n1"}.get(text)

    def test_semicolon_and_ru_dates(self):
        path = self.write(
            "olympiad;kind;title;starts_at;ends_at;profile;source_url\n"
            "Физтех;registration;Регистрация;01.10.2026;15.11.2026;;https://olymp.mipt.ru\n"
            "НТО;qualifying;;2026-11-20;;искусственный интеллект;\n"
            ";;;;;;\n"
        )
        rows = read_stages_csv(path, self.resolve, "2026/27")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].starts_at, date(2026, 10, 1))
        self.assertEqual(rows[0].ends_at, date(2026, 11, 15))
        self.assertEqual(rows[1].title, "отборочный этап")
        self.assertEqual(rows[1].profile, "искусственный интеллект")
        self.assertEqual(rows[1].academic_year, "2026/27")

    def test_comma_delimiter(self):
        path = self.write("olympiad,kind,starts_at\nФизтех,final,2027-02-20\n")
        rows = read_stages_csv(path, self.resolve, "2026/27")
        self.assertEqual(rows[0].kind, "final")

    def test_errors_are_collected(self):
        path = self.write(
            "olympiad;kind;starts_at;ends_at\n"
            "Неизвестная;final;2027-02-20;\n"
            "Физтех;финал;2027-02-20;\n"
            "Физтех;final;2027-02-30;\n"
            "Физтех;final;2027-02-20;2027-02-10\n"
        )
        with self.assertRaises(ValueError) as ctx:
            read_stages_csv(path, self.resolve, "2026/27")
        message = str(ctx.exception)
        for line in ("Строка 2", "Строка 3", "Строка 4", "Строка 5"):
            self.assertIn(line, message)

    def test_missing_columns(self):
        path = self.write("name;date\nФизтех;2027-02-20\n")
        with self.assertRaises(ValueError):
            read_stages_csv(path, self.resolve, "2026/27")


if __name__ == "__main__":
    unittest.main()
