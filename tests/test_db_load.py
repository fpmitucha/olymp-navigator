"""Тесты генерации SQL и загрузки в настоящий PostgreSQL.

Интеграционные тесты запускаются, только если задана TEST_DATABASE_URL
(отдельная пустая база, тесты пересоздают в ней схему public):
    TEST_DATABASE_URL=postgresql://postgres@localhost:5432/olymp_test python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path

from app.db.sql_loader import rsr_to_sql, sql_literal
from app.parsers.rsr import parse_rsr_html

FIXTURE = Path(__file__).parent / "fixtures" / "rsr_2026_sample.html"
TEST_DB = os.environ.get("TEST_DATABASE_URL")


class SqlLiteralTest(unittest.TestCase):
    def test_values(self):
        self.assertEqual(sql_literal(None), "NULL")
        self.assertEqual(sql_literal(3), "3")
        self.assertEqual(sql_literal("д'Артаньян"), "'д''Артаньян'")
        self.assertEqual(sql_literal([]), "'{}'::text[]")
        self.assertEqual(sql_literal(["a", "b'c"]), "ARRAY['a', 'b''c']::text[]")

    def test_null_byte_rejected(self):
        with self.assertRaises(ValueError):
            sql_literal("a\x00b")


class SqlScriptTest(unittest.TestCase):
    def test_script_shape(self):
        rsr = parse_rsr_html(FIXTURE.read_text(encoding="utf-8"), url="https://rsr-olymp.ru/")
        sql = rsr_to_sql(rsr)
        self.assertTrue(sql.splitlines()[3] == "BEGIN;")
        self.assertIn("COMMIT;", sql)
        self.assertEqual(sql.count("INSERT INTO olympiad "), 11)
        self.assertEqual(sql.count("INSERT INTO olympiad_profile"), 24)
        self.assertIn("DELETE FROM olympiad_profile WHERE academic_year = '2026/27' AND source = 'rsr';", sql)


@unittest.skipUnless(TEST_DB and shutil.which("psql"), "нужны TEST_DATABASE_URL и psql")
class PostgresLoadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.db.psql import migrate, run_psql

        cls.run_psql = staticmethod(run_psql)
        run_psql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;", database_url=TEST_DB)
        cls.applied = migrate(TEST_DB)
        cls.rsr = parse_rsr_html(FIXTURE.read_text(encoding="utf-8"), url="https://rsr-olymp.ru/")

    def scalar(self, sql: str) -> str:
        return self.run_psql(sql, database_url=TEST_DB, tuples_only=True).strip()

    def test_migrations_applied_once(self):
        from app.db.psql import migrate

        self.assertIn("001_init", self.applied)
        self.assertEqual(migrate(TEST_DB), [])

    def test_load_is_idempotent(self):
        sql = rsr_to_sql(self.rsr)
        self.run_psql(sql, database_url=TEST_DB)
        self.run_psql(sql, database_url=TEST_DB)
        self.assertEqual(self.scalar("SELECT count(*) FROM olympiad"), "11")
        self.assertEqual(self.scalar("SELECT count(*) FROM olympiad_profile WHERE academic_year = '2026/27'"), "24")
        self.assertEqual(
            self.scalar(
                "SELECT level FROM olympiad_profile p JOIN olympiad o ON o.id = p.olympiad_id "
                "WHERE o.key LIKE '%росатом%' AND p.profile = 'физика'"
            ),
            "1",
        )
        # поиск по предмету через GIN-индекс
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM olympiad_profile WHERE subjects @> "
                "ARRAY['фотоника, приборостроение, оптические и биотехнические системы и технологии']"
            ),
            "6",
        )

    def test_older_year_does_not_overwrite_name(self):
        self.run_psql(rsr_to_sql(self.rsr), database_url=TEST_DB)
        old = parse_rsr_html(
            FIXTURE.read_text(encoding="utf-8").replace("XIX Южно-Российская", "XVIII Южно-Российская"),
            academic_year="2025/26",
        )
        self.run_psql(rsr_to_sql(old), database_url=TEST_DB)
        name = self.scalar("SELECT name FROM olympiad WHERE key LIKE 'южно-российская%'")
        self.assertTrue(name.startswith("XIX"), name)
        self.assertEqual(self.scalar("SELECT count(DISTINCT academic_year) FROM olympiad_profile"), "2")
        self.assertEqual(self.scalar("SELECT count(*) FROM olympiad"), "11")

    def test_vsosh_load(self):
        from app.parsers.vsosh import vsosh_list

        self.run_psql(rsr_to_sql(self.rsr), database_url=TEST_DB)
        sql = rsr_to_sql(vsosh_list("2026/27"), source="vsosh", is_vsosh=True)
        self.run_psql(sql, database_url=TEST_DB)
        self.run_psql(sql, database_url=TEST_DB)
        self.assertEqual(self.scalar("SELECT count(*) FROM olympiad WHERE is_vsosh"), "1")
        self.assertEqual(self.scalar("SELECT count(*) FROM olympiad_profile WHERE source = 'vsosh'"), "24")
        self.assertEqual(self.scalar("SELECT count(*) FROM olympiad_profile WHERE source = 'rsr' AND academic_year = '2026/27'"), "24")
        self.assertEqual(
            self.scalar("SELECT count(*) FROM olympiad_profile WHERE source = 'vsosh' AND level IS NULL"), "24")


if __name__ == "__main__":
    unittest.main()
