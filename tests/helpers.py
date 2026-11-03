"""Маленькие перечни для тестов каталога и бота."""

from __future__ import annotations

from app.parsers.rsr import Olympiad, Profile, RsrList, olympiad_key


def profile(name: str, level: int | None, subjects: list[str] | None = None, website: str | None = None) -> Profile:
    subjects = subjects or [name]
    return Profile(profile=name, subjects_raw=", ".join(subjects), subjects=subjects, level=level, website=website)


def olympiad(name: str, *profiles: Profile, number: int = 1) -> Olympiad:
    return Olympiad(number=number, name=name, key=olympiad_key(name), profiles=list(profiles))


def rsr_list(year: str, *olympiads: Olympiad) -> RsrList:
    return RsrList(academic_year=year, source_url=None, parsed_at="", olympiads=list(olympiads), warnings=[])


def sample_lists() -> list[RsrList]:
    """Три года перечня: переименования, выбывшая олимпиада, две разные олимпиады с одним названием."""
    fiztech = lambda m, f: olympiad(  # noqa: E731
        "Олимпиада школьников «Физтех»",
        profile("математика", m, website="https://olymp.mipt.ru"),
        profile("физика", f, website="https://olymp.mipt.ru"),
    )
    nto = lambda title: olympiad(  # noqa: E731
        title,
        profile("искусственный интеллект", 2, ["математика и механика", "информатика и вычислительная техника"],
                website="https://ntcontest.ru"),
        profile("летающая робототехника", 3, ["машиностроение"], website="https://ntcontest.ru"),
    )
    arch_south = olympiad("XIX Южно-Российская олимпиада «Архитектура и искусство»",
                          profile("рисунок", 1, ["архитектура"], website="http://www.raai.sfedu.ru/"))
    arch_tyumen = olympiad("Межрегиональная олимпиада школьников «Архитектура и искусство» (рисунок, композиция)",
                           profile("рисунок", 3, ["архитектура"], website="https://www.tyuiu.ru/"))
    return [
        rsr_list(
            "2024/25",
            fiztech(2, 1),
            nto("Всероссийская междисциплинарная олимпиада школьников 8-11 класса «Национальная технологическая олимпиада»"),
            arch_south, arch_tyumen,
            olympiad("Олимпиада школьников «Робофест»", profile("физика", 3, website="https://robofest.ru")),
            olympiad("Объединенная межвузовская математическая олимпиада школьников", profile("математика", 2)),
        ),
        rsr_list(
            "2025/26",
            fiztech(2, 1),
            nto("Всероссийская междисциплинарная олимпиада школьников 8–11 классов «Национальная технологическая олимпиада»"),
            arch_south, arch_tyumen,
            olympiad("Олимпиада школьников «Робофест»", profile("физика", 3, website="https://robofest.ru")),
            olympiad("Объединенная межвузовская олимпиада школьников", profile("математика", 2)),
        ),
        rsr_list(
            "2026/27",
            fiztech(1, 1),
            nto("Всероссийская междисциплинарная олимпиада школьников 8–11 классов «Национальная технологическая олимпиада»"),
            arch_south,
            olympiad("Объединенная межвузовская олимпиада школьников", profile("математика", 2), profile("физика", 3)),
            olympiad("Олимпиада школьников «Ломоносов»", profile("математика", 1), profile("химия", 1)),
        ),
    ]


def sample_catalog():
    from app.catalog import Catalog
    from app.parsers.vsosh import vsosh_list

    return Catalog(sample_lists(), [vsosh_list("2026/27")])
