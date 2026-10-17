"""Что изменилось в льготах вуза: прошлый год (или прошлая версия файла) → новый документ.

Сравниваем «по смыслу», а не построчно: ключ — олимпиада, профиль, диплом. Для ключа собираем,
какие льготы есть (БВИ / 100 баллов ЕГЭ / 100 за ДВИ), минимальный балл ЕГЭ и число программ.
Отсюда: новые льготы, убранные, «БВИ заменили на 100 баллов», «порог ЕГЭ вырос».
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.benefits import Rule, UniversityBenefits

Key = tuple[str, str, str]  # (id олимпиады, профиль или «*», диплом)


@dataclass
class Entry:
    benefits: set[str] = field(default_factory=set)
    min_score: int | None = None
    programs: int = 0


@dataclass
class Change:
    olympiad_id: str
    olympiad: str
    profile: str
    diploma: str
    kind: str          # added | removed | bvi_to_100 | 100_to_bvi | stricter | softer
    detail: str = ""


def snapshot(rules: list[Rule]) -> dict[Key, Entry]:
    result: dict[Key, Entry] = {}
    for r in rules:
        # «победителям и призёрам» = два ключа: иначе разделение на «победителям» и «призёрам» выглядело бы
        # как «убрали одно, добавили другое»
        diplomas = ["winner", "prize"] if r.diploma == "any" else [r.diploma]
        for profile, diploma in ((p, d) for p in r.profiles or ["*"] for d in diplomas):
            e = result.setdefault((r.olympiad.id, profile, diploma), Entry())
            e.benefits.add(r.benefit)
            if r.confirm_min_score:
                e.min_score = min(e.min_score or 100, r.confirm_min_score)
            e.programs += len(r.programs or [])
    return result


def dump_snapshot(rules: list[Rule]) -> str:
    """Для хранения в базе бота: прошлую версию льгот вуза сравним со следующей."""
    snap = snapshot(rules)
    names = {r.olympiad.id: r.olympiad.name for r in rules}
    return json.dumps({"names": names, "rows": [[*k, sorted(e.benefits), e.min_score, e.programs]
                                                for k, e in sorted(snap.items())]}, ensure_ascii=False)


def load_snapshot(data: str) -> tuple[dict[Key, Entry], dict[str, str]]:
    raw = json.loads(data)
    rows = {(o, p, d): Entry(set(b), s, n) for o, p, d, b, s, n in raw["rows"]}
    return rows, raw.get("names", {})


def diff(old: dict[Key, Entry], new: dict[Key, Entry], names: dict[str, str]) -> list[Change]:
    changes: list[Change] = []
    for key in sorted(set(old) | set(new)):
        o_id, profile, diploma = key
        name = names.get(o_id, o_id)
        before, after = old.get(key), new.get(key)
        if before is None:
            changes.append(Change(o_id, name, profile, diploma, "added", ", ".join(sorted(after.benefits))))
        elif after is None:
            changes.append(Change(o_id, name, profile, diploma, "removed", ", ".join(sorted(before.benefits))))
        else:
            if "bvi" in before.benefits and "bvi" not in after.benefits:
                changes.append(Change(o_id, name, profile, diploma, "bvi_to_100"))
            elif "bvi" not in before.benefits and "bvi" in after.benefits:
                changes.append(Change(o_id, name, profile, diploma, "100_to_bvi"))
            if before.min_score and after.min_score and after.min_score != before.min_score:
                kind = "stricter" if after.min_score > before.min_score else "softer"
                changes.append(Change(o_id, name, profile, diploma, kind,
                                      f"ЕГЭ от {before.min_score} → от {after.min_score}"))
    return changes


def diff_universities(old: UniversityBenefits, new: UniversityBenefits) -> list[Change]:
    names = {r.olympiad.id: r.olympiad.name for r in old.rules + new.rules}
    return diff(snapshot(old.rules), snapshot(new.rules), names)


KIND_TITLES = {
    "bvi_to_100": "⚠️ БВИ больше нет",
    "removed": "➖ Льготу убрали",
    "stricter": "📈 Порог ЕГЭ вырос",
    "added": "➕ Новые льготы",
    "100_to_bvi": "🏅 Теперь БВИ",
    "softer": "📉 Порог ЕГЭ снизился",
}
DIPLOMA_SHORT = {"winner": "победители", "prize": "призёры"}


def summarize(changes: list[Change], *, focus: set[str] | None = None, limit: int = 12) -> list[str]:
    """Строки для сообщения: сначала изменения по олимпиадам из focus (подписки пользователя),
    сначала плохие новости. Для каждого вида — до limit строк и счётчик остального."""
    from app.bot.texts import escape, short_name

    lines: list[str] = []
    order = list(KIND_TITLES)
    for kind in order:
        items = [c for c in changes if c.kind == kind]
        if not items:
            continue
        items.sort(key=lambda c: (focus is not None and c.olympiad_id not in focus, c.olympiad, c.profile))
        lines.append(f"<b>{KIND_TITLES[kind]}</b> ({len(items)})")
        for c in items[:limit]:
            star = "⭐ " if focus and c.olympiad_id in focus else ""
            profile = "" if c.profile == "*" else f", {c.profile}"
            detail = f" — {c.detail}" if c.detail and kind in ("stricter", "softer") else ""
            lines.append(f"• {star}{escape(short_name(c.olympiad, 50))}{escape(profile)} "
                         f"({DIPLOMA_SHORT[c.diploma]}){escape(detail)}")
        if len(items) > limit:
            lines.append(f"  …и ещё {len(items) - limit}")
        lines.append("")
    return lines
