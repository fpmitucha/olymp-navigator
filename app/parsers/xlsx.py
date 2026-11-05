"""Чтение таблиц .xlsx стандартной библиотекой (без openpyxl).

XLSX — zip-архив с XML. Нужны общие строки (xl/sharedStrings.xml) и листы
(xl/worksheets/sheetN.xml). Возвращаем строки как словари «буква колонки → текст».
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_COL_RE = re.compile(r"[A-Z]+")


def read_xlsx(data: bytes, sheet: int = 1) -> list[dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
        root = ElementTree.fromstring(z.read(f"xl/worksheets/sheet{sheet}.xml"))
    rows: list[dict[str, str]] = []
    for row in root.iter(f"{{{NS['m']}}}row"):
        cells: dict[str, str] = {}
        for c in row.findall("m:c", NS):
            column = _COL_RE.match(c.get("r", "")).group(0)
            kind = c.get("t")
            v = c.find("m:v", NS)
            if kind == "s" and v is not None:
                value = shared[int(v.text)]
            elif kind == "inlineStr":
                value = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
            else:
                value = v.text if v is not None and v.text is not None else ""
            cells[column] = value
        rows.append(cells)
    return rows


def write_xlsx(rows: list[dict[str, str]]) -> bytes:
    """Минимальный xlsx с inline-строками — для тестов."""
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    sheet_rows = []
    for i, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{col}{i}" t="inlineStr"><is><t>{esc(val)}</t></is></c>' for col, val in sorted(row.items()))
        sheet_rows.append(f'<row r="{i}">{cells}</row>')
    sheet = (f'<worksheet xmlns="{NS["m"]}"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()
