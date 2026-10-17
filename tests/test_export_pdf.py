import tempfile
import unittest
from pathlib import Path

try:
    import reportlab  # noqa: F401
    from PIL import Image

    HAS_REPORTLAB = True
except ImportError:  # pragma: no cover
    HAS_REPORTLAB = False


@unittest.skipUnless(HAS_REPORTLAB, "reportlab не установлен")
class ExportPdfTest(unittest.TestCase):
    def setUp(self):
        from app.benefits import BenefitsCatalog, parse_file
        from tests.helpers import sample_catalog
        from tests.test_benefits import university

        self.catalog = sample_catalog()
        self.benefits = BenefitsCatalog([parse_file(university(), self.catalog, "t.json")])
        self.olympiad = self.catalog.search("Физтех")[0].olympiad

    def build(self, **kwargs) -> bytes:
        from app.export_pdf import benefits_pdf

        return benefits_pdf(self.olympiad, self.benefits.for_olympiad(self.olympiad.id), 2027, **kwargs)

    def test_with_and_without_avatar(self):
        plain = self.build(avatar=None)
        self.assertTrue(plain.startswith(b"%PDF"))
        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            Image.new("RGB", (300, 200), "#3D3FA8").save(avatar)
            with_avatar = self.build(avatar=avatar)
        self.assertTrue(with_avatar.startswith(b"%PDF"))
        self.assertGreater(len(with_avatar), len(plain))  # картинка водяного знака встроена

    def test_long_program_lists_do_not_break_layout(self):
        from dataclasses import replace

        u, rules = self.benefits.for_olympiad(self.olympiad.id)[0]
        long_rules = [replace(r, programs=[f"01.03.{i:02d} Очень длинное название программы {i}" * 2
                                           for i in range(300)]) for r in rules]
        from app.export_pdf import benefits_pdf

        self.assertTrue(benefits_pdf(self.olympiad, [(u, long_rules)], 2027, avatar=None).startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
