"""Всероссийская олимпиада школьников (ВсОШ).

ВсОШ не входит в перечень РСОШ: её проводит Минпросвещения, уровня у неё нет,
а победители и призёры заключительного этапа получают БВИ по закону
на направления, соответствующие профилю олимпиады.

Список предметов на 2026/27 — 24 предмета (сверено 15.10.2026 по
https://lenta.ru/articles/2026/09/17/vserossiyskaya-olimpiada-shkolnikov-v-2026-2027-godu/).
Каждый год сверяйте со свежим приказом Минпросвещения.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.parsers.rsr import Olympiad, Profile, RsrList, olympiad_key

VSOSH_NAME = "Всероссийская олимпиада школьников"
VSOSH_WEBSITE: str | None = None  # TODO: указать официальный сайт ВсОШ после сверки

FOREIGN_LANGUAGES = (
    "английский язык", "испанский язык", "итальянский язык",
    "китайский язык", "немецкий язык", "французский язык",
)

VSOSH_SUBJECTS: dict[str, tuple[str, ...]] = {
    "2026/27": (
        "английский язык", "астрономия", "биология", "география", "информатика",
        "искусство (мировая художественная культура)", "испанский язык", "история",
        "итальянский язык", "китайский язык", "литература", "математика", "немецкий язык",
        "основы безопасности и защиты Родины", "обществознание", "право", "русский язык",
        "труд (технология)", "физика", "физическая культура", "французский язык", "химия",
        "экология", "экономика",
    ),
}


def vsosh_list(academic_year: str) -> RsrList:
    """Профили ВсОШ на учебный год в том же формате, что и перечень РСОШ."""
    if academic_year not in VSOSH_SUBJECTS:
        known = ", ".join(sorted(VSOSH_SUBJECTS))
        raise KeyError(f"Нет списка предметов ВсОШ на {academic_year}. Известны: {known}")
    profiles = [
        Profile(
            profile=subject,
            subjects_raw=subject,
            subjects=["иностранный язык"] if subject in FOREIGN_LANGUAGES else [subject],
            level=None,
            website=VSOSH_WEBSITE,
        )
        for subject in VSOSH_SUBJECTS[academic_year]
    ]
    return RsrList(
        academic_year=academic_year,
        source_url="https://lenta.ru/articles/2026/09/17/vserossiyskaya-olimpiada-shkolnikov-v-2026-2027-godu/",
        parsed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        olympiads=[Olympiad(number=None, name=VSOSH_NAME, key=olympiad_key(VSOSH_NAME), profiles=profiles)],
        warnings=[],
    )
