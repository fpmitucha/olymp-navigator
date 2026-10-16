"""PDF со льготами олимпиады — «в нашем стиле»: шапка бота, цветные метки льгот, полные списки программ.

Telegram обрезает длинные сообщения, а в PDF помещается всё: каждый вуз, каждое правило, все направления,
условия по ЕГЭ и ссылка на первоисточник. Используется в боте (кнопка «📄 PDF») и в CLI.
"""

from __future__ import annotations

import io
import re
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.benefits import BENEFIT_TYPES, DIPLOMAS, Rule, UniversityBenefits
from app.catalog import CatalogOlympiad

FONTS = Path(__file__).parent / "assets" / "fonts"
BRAND = "Бвишечка"
BRAND_LINE = "навигатор по олимпиадам и льготам вузов · t.me/bvishechka_bot"

# Палитра логотипа: графитовые линии и золотая звезда; у каждой льготы свой цвет метки
INK = colors.HexColor("#1F1F22")
GOLD = colors.HexColor("#C0952D")
MUTED = colors.HexColor("#6B7080")
RULE_LINE = colors.HexColor("#E3E5EC")
BRAND_COLOR = colors.HexColor("#232326")
BADGE = {
    "bvi": colors.HexColor("#1E8E5A"),
    "score100": colors.HexColor("#2463D6"),
    "dvi100": colors.HexColor("#7A3FD1"),
}
BADGE_TINT = {
    "bvi": colors.HexColor("#E7F4EC"),
    "score100": colors.HexColor("#E7EEFB"),
    "dvi100": colors.HexColor("#F0E9FB"),
}
BADGE_TEXT = {"bvi": "БВИ", "score100": "100 ЕГЭ", "dvi100": "100 ДВИ"}
NOTE_BG = colors.HexColor("#FFF6DD")
PROGRAMS_CHARS_PER_ROW = 700  # строка таблицы не может переходить на новую страницу — режем длинные списки
NOTE_BORDER = colors.HexColor("#E8B23A")

CLOSING_TITLE = "Поможем пройти весь путь"
CLOSING_TEXT = ("Сопроводим на всех этапах: подберём олимпиады под твой вуз и направление, напомним о регистрации "
                "и каждом этапе, подскажем, как готовиться, — вплоть до получения нужного диплома и поступления.")
LOGO = Path(__file__).parent / "assets" / "logo.png"
WATERMARK_WIDTH = 150 * mm
WATERMARK_OPACITY = 0.08

_registered = False
_avatar_cache: dict[tuple[str, float], tuple] = {}


def avatar_path() -> Path | None:
    """Логотип для водяного знака: data/avatar.png (своя картинка) → логотип из репозитория →
    аватарка, которую бот скачал из Telegram (data/avatar.jpg)."""
    from app.config import settings

    candidates = [Path(settings.data_dir) / "avatar.png", LOGO, Path(settings.data_dir) / "avatar.jpg"]
    return next((p for p in candidates if p.exists()), None)


def _faded_avatar(path: Path, opacity: float):
    """(ImageReader полупрозрачной круглой аватарки, ширина/высота). Pillow ставится вместе с reportlab."""
    key = (str(path), opacity, path.stat().st_mtime)
    if key not in _avatar_cache:
        from PIL import Image, ImageOps
        from reportlab.lib.utils import ImageReader

        img = Image.open(path).convert("RGB")
        side = min(img.size)
        img = img.crop(((img.width - side) // 2, (img.height - side) // 2,
                        (img.width + side) // 2, (img.height + side) // 2)).resize((800, 800))
        # белый фон логотипа → прозрачный: остаются только линии и звезда
        alpha = ImageOps.invert(img.convert("L")).point(lambda v: int(min(255, v * 1.6) * opacity))
        img = img.convert("RGBA")
        img.putalpha(alpha)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)
        _avatar_cache[key] = (ImageReader(buf), 1.0)
    return _avatar_cache[key][0]


def _fonts() -> None:
    global _registered
    if not _registered:
        pdfmetrics.registerFont(TTFont("Sans", str(FONTS / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("Sans-Bold", str(FONTS / "DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFontFamily("Sans", normal="Sans", bold="Sans-Bold", italic="Sans", boldItalic="Sans-Bold")
        _registered = True


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName="Sans", fontSize=9, leading=12, textColor=INK, alignment=TA_LEFT)
    return {
        "title": ParagraphStyle("title", parent=base, fontName="Sans-Bold", fontSize=17, leading=21),
        "subtitle": ParagraphStyle("subtitle", parent=base, fontSize=10.5, leading=14, textColor=MUTED),
        "uni": ParagraphStyle("uni", parent=base, fontName="Sans-Bold", fontSize=13, leading=16, keepWithNext=1),
        "status": ParagraphStyle("status", parent=base, fontSize=8.5, textColor=MUTED, keepWithNext=1),
        "cell": ParagraphStyle("cell", parent=base, fontSize=8.3, leading=10.6),
        "small": ParagraphStyle("small", parent=base, fontSize=7.6, leading=9.8, textColor=MUTED),
        "badge": ParagraphStyle("badge", parent=base, fontName="Sans-Bold", fontSize=8, leading=10),
        "note": ParagraphStyle("note", parent=base, fontSize=8.6, leading=11.5),
        "head": ParagraphStyle("head", parent=base, fontName="Sans-Bold", fontSize=7.6, leading=9.5,
                               textColor=MUTED),
    }


def _plain(html: str) -> str:
    """Текст бота (HTML и эмодзи) → обычная строка для PDF."""
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"[\U0001F000-\U0001FFFF☀-➿️]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _levels(levels: list[int] | None) -> str:
    if not levels or set(levels) == {1, 2, 3}:
        return ""
    roman = {1: "I", 2: "II", 3: "III"}
    lv = sorted(levels)
    return roman[lv[0]] + (f"–{roman[lv[-1]]}" if len(lv) > 1 else "") + " уровень"


def _who(r: Rule) -> str:
    parts = [f"<b>{escape(DIPLOMAS[r.diploma])}</b>"]
    if r.profiles:
        parts.append(escape(", ".join(r.profiles)))
    if _levels(r.levels):
        parts.append(_levels(r.levels))
    if r.grades:
        parts.append("за " + " или ".join(str(g) for g in r.grades) + " класс")
    return "<br/>".join(parts)


def _conditions(r: Rule) -> str:
    parts = []
    if r.confirm_min_score:
        parts.append(f"ЕГЭ от <b>{r.confirm_min_score}</b>: {escape(r.confirm_subject or 'предмет профиля')}")
    elif r.olympiad.is_vsosh:
        parts.append("без условия по ЕГЭ")
    if r.score_for:
        parts.append(f"100 баллов: {escape(r.score_for)}")
    if r.diploma_since:
        parts.append(f"диплом не раньше {r.diploma_since} года")
    if r.note:
        parts.append(f'<font color="#6B7080">{escape(r.note)}</font>')
    return "<br/>".join(parts) or "—"


def _programs(r: Rule) -> str:
    if r.programs:
        return escape("; ".join(r.programs))
    return '<font color="#6B7080">направления по профилю олимпиады</font>'


def _university_block(u: UniversityBenefits, rules: list[Rule], admission_year: int, st) -> list:
    flow = [Paragraph(escape(u.university), st["uni"]),
            Paragraph(escape(u.full_name) + " · " + escape(u.status_label(admission_year)), st["status"]),
            Spacer(1, 2 * mm)]
    order = sorted(_merge_profiles(rules), key=lambda r: (list(BENEFIT_TYPES).index(r.benefit),
                                                          (r.profiles or [""])[0], r.diploma != "any"))
    data = [[Paragraph("Льгота", st["head"]), Paragraph("Кому", st["head"]), Paragraph("Условия", st["head"]),
             Paragraph("Направления и программы", st["head"])]]
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE_LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    for r in order:
        # длинный список программ — на несколько строк таблицы: одна строка не может переходить на новую страницу
        chunks = _chunks(r.programs) if r.programs else [None]
        for n, chunk in enumerate(chunks):
            row = len(data)
            programs = escape("; ".join(chunk)) if chunk else _programs(r)
            badge = f'<font color="{_hex(BADGE[r.benefit])}">{BADGE_TEXT[r.benefit]}</font>'
            if n == 0:
                data.append([Paragraph(badge, st["badge"]), Paragraph(_who(r), st["cell"]),
                             Paragraph(_conditions(r), st["cell"]), Paragraph(programs, st["cell"])])
            else:
                data.append([Paragraph("", st["badge"]), Paragraph('<font color="#6B7080">продолжение</font>',
                                                                   st["cell"]), "", Paragraph(programs, st["cell"])])
                style.append(("LINEABOVE", (0, row), (-1, row), 0, colors.white))
            style += [("BACKGROUND", (0, row), (0, row), BADGE_TINT[r.benefit]),
                      ("LINEBEFORE", (0, row), (0, row), 2.5, BADGE[r.benefit])]
    table = Table(data, colWidths=[18 * mm, 34 * mm, 40 * mm, None], repeatRows=1, splitByRow=1)
    table.setStyle(TableStyle(style))
    flow.append(table)
    source = (f'Источник: <link href="{escape(u.source_url)}" color="#9A7420">'
              f"{escape(u.source_title or u.source_url)}</link>, проверено {u.checked_at.strftime('%d.%m.%Y')}")
    flow += [Spacer(1, 1.5 * mm), Paragraph(source, st["small"]), Spacer(1, 7 * mm)]
    return flow


def _merge_profiles(rules: list[Rule]) -> list[Rule]:
    """Правила, которые отличаются только профилем, — в одну строку с несколькими профилями."""
    from dataclasses import replace

    merged: dict[tuple, Rule] = {}
    for r in rules:
        key = (r.benefit, r.diploma, tuple(r.levels or ()), tuple(r.grades or ()), r.confirm_min_score,
               r.confirm_subject, r.score_for, r.note, r.diploma_since, tuple(r.programs or ()), r.programs is None)
        if key in merged and merged[key].profiles is not None and r.profiles is not None:
            profiles = merged[key].profiles + [p for p in r.profiles if p not in merged[key].profiles]
            merged[key] = replace(merged[key], profiles=profiles)
        elif key not in merged:
            merged[key] = r
        else:  # одно из правил — на любой профиль: оно шире
            merged[key] = replace(merged[key], profiles=None)
    return list(merged.values())


def _hex(color) -> str:
    return "#" + color.hexval()[2:]


def _chunks(programs: list[str]) -> list[list[str]]:
    chunks, current, size = [], [], 0
    for p in programs:
        if current and size + len(p) > PROGRAMS_CHARS_PER_ROW:
            chunks.append(current)
            current, size = [], 0
        current.append(p)
        size += len(p) + 2
    return chunks + [current] if current else chunks


def _closing_box(st, width: float) -> Table:
    """Финальный блок: сопровождаем до диплома."""
    title = ParagraphStyle("closing_title", parent=st["uni"], textColor=GOLD, fontSize=13.5, leading=17)
    body = ParagraphStyle("closing_body", parent=st["note"], textColor=colors.white, fontSize=9.5, leading=13)
    link = ParagraphStyle("closing_link", parent=body, fontName="Sans-Bold")
    box = Table([[Paragraph(CLOSING_TITLE, title)], [Paragraph(CLOSING_TEXT, body)],
                 [Paragraph('Пиши боту: <link href="https://t.me/bvishechka_bot" color="#E2BE62">'
                            "t.me/bvishechka_bot</link>", link)]], colWidths=[width])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BRAND_COLOR),
        ("LINEBEFORE", (0, 0), (0, -1), 3, GOLD),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, 0), 11), ("BOTTOMPADDING", (0, -1), (-1, -1), 11),
        ("TOPPADDING", (0, 1), (-1, -1), 3),
    ]))
    return box


def _note_box(lines: list[str], st, width: float) -> Table:
    box = Table([[Paragraph("<br/>".join(escape(x) for x in lines), st["note"])]], colWidths=[width])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NOTE_BG),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, NOTE_BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return box


def benefits_pdf(o: CatalogOlympiad, entries: list[tuple[UniversityBenefits, list[Rule]]], admission_year: int,
                 *, subtitle: str = "", notes: list[str] | None = None, today: date | None = None,
                 avatar: Path | None = None) -> bytes:
    """PDF: олимпиада → по каждому вузу таблица правил. notes — предупреждения (текст бота, HTML можно)."""
    _fonts()
    st = _styles()
    today = today or date.today()
    buf = io.BytesIO()
    margin = 15 * mm
    name = "Всероссийская олимпиада школьников (ВсОШ)" if o.is_vsosh else o.name

    avatar = avatar if avatar is not None else avatar_path()
    watermark = header_icon = None
    if avatar:
        try:
            watermark = _faded_avatar(avatar, WATERMARK_OPACITY)
            header_icon = _faded_avatar(avatar, 1.0)
        except Exception:  # noqa: BLE001 — битая картинка не должна ломать PDF
            watermark = header_icon = None

    def decorate(canvas, doc):
        canvas.saveState()
        w, h = A4
        if watermark is not None:  # водяной знак — под содержимым страницы
            canvas.drawImage(watermark, (w - WATERMARK_WIDTH) / 2, (h - WATERMARK_WIDTH) / 2 - 8 * mm,
                             WATERMARK_WIDTH, WATERMARK_WIDTH, mask="auto")
        # шапка: логотип, название, золотая линия
        x = margin
        if header_icon is not None:
            canvas.drawImage(header_icon, margin - 1.5 * mm, h - 13 * mm, 12 * mm, 12 * mm, mask="auto")
            x += 11 * mm
        canvas.setFillColor(INK)
        canvas.setFont("Sans-Bold", 12)
        canvas.drawString(x, h - 8.6 * mm, BRAND)
        canvas.setFillColor(MUTED)
        canvas.setFont("Sans", 8)
        canvas.drawString(x + pdfmetrics.stringWidth(BRAND, "Sans-Bold", 12) + 3 * mm, h - 8.6 * mm, BRAND_LINE)
        canvas.setStrokeColor(GOLD)
        canvas.setLineWidth(0.8)
        canvas.line(margin, h - 13.5 * mm, w - margin, h - 13.5 * mm)
        canvas.setFillColor(MUTED)
        canvas.setFont("Sans", 7)
        canvas.drawString(margin, 8 * mm, f"Сформировано {today.strftime('%d.%m.%Y')}. Не является официальным "
                                          "документом — всегда сверяйте с правилами приёма вуза.")
        canvas.drawRightString(w - margin, 8 * mm, f"стр. {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=20 * mm,
                            bottomMargin=15 * mm, title=f"Льготы: {name}", author=BRAND)
    width = A4[0] - 2 * margin
    flow = [Paragraph(escape(name), st["title"]), Spacer(1, 1.5 * mm),
            Paragraph(escape(subtitle or f"Льготы в вузах · приём {admission_year}"), st["subtitle"]),
            Spacer(1, 5 * mm)]
    plain_notes = [_plain(n) for n in notes or [] if _plain(n)]
    if plain_notes:
        flow += [_note_box(plain_notes, st, width), Spacer(1, 6 * mm)]
    if not entries:
        flow.append(Paragraph("Ни один из собранных вузов не даёт льгот по этому запросу.", st["note"]))
    legend = " · ".join(f'<font color="{_hex(BADGE[k])}"><b>{BADGE_TEXT[k]}</b></font> — '
                        f"{escape(BENEFIT_TYPES[k])}" for k in BADGE)
    flow += [Paragraph(legend, st["small"]), Spacer(1, 5 * mm)]
    for u, rules in entries:
        flow += _university_block(u, rules, admission_year, st)
    flow.append(_closing_box(st, width))
    doc.build(flow, onFirstPage=decorate, onLaterPages=decorate)
    return buf.getvalue()


def pdf_filename(o: CatalogOlympiad, suffix: str = "") -> str:
    base = "vsosh" if o.is_vsosh else re.sub(r"[^a-z0-9]+", "-", _translit(o.name.lower())).strip("-")[:50]
    return f"lgoty-{base}{('-' + suffix) if suffix else ''}.pdf"


_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def _translit(text: str) -> str:
    return "".join(_TR.get(ch, ch) for ch in text)
