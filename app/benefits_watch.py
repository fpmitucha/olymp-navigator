"""Слежение за публикацией особых прав вузов на следующий приём.

Вузы обязаны выложить особые права для олимпиадников до 1 ноября. Источники — benefits/watch/sources.json:
* page — страница приёма: ищем на ней новые ссылки, где есть год приёма и слова про олимпиады / особые права;
* probe — адрес файла по шаблону прошлого года (cpk.msu.ru/files/{year}/olymp_benefits.pdf): появился — нашли;
* hash — документ по постоянному адресу: изменилось содержимое — сообщаем;
* marker — текст на странице («поступающих … в {year} году»).

Первый запуск только запоминает текущее состояние (иначе все старые ссылки посчитались бы новыми).
Что нашли — уходит подписчикам вуза и администратору, а данные в бот добавляем после проверки.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.fetch import PageGone

log = logging.getLogger(__name__)

STATE_KEY = "benefits_watch_state"
KEYWORDS = re.compile(r"олимпиад|особ\w* прав|льгот|без вступительных|БВИ|olymp|olimp|bvi|osob|pravila", re.I)


@dataclass
class Finding:
    university: str
    url: str
    title: str


@dataclass
class WatchReport:
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    baseline: bool = False


def load_sources(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["sources"]


def year_links(html: str, base_url: str, year: int) -> dict[str, str]:
    """Ссылки страницы, где в адресе или тексте есть год приёма и слово про олимпиады / особые права."""
    soup = BeautifulSoup(html, "lxml")
    found = {}
    for a in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", a.get_text(" ")).strip()
        href = urljoin(base_url, a["href"])
        both = f"{href} {text}"
        if str(year) in both and KEYWORDS.search(both):
            found[href] = text or href
    return found


def check_sources(sources: list[dict], year: int, state: dict, fetch_html: Callable[[str], str],
                  fetch_bytes: Callable[[str], bytes]) -> tuple[WatchReport, dict]:
    """Один обход. state — что видели раньше ({вуз: {"links": [...], "probes": [...], "hash": {...}, "marker": bool}})."""
    report = WatchReport(baseline=not state)
    new_state = {u: dict(v) for u, v in state.items()}
    for source in sources:
        name = source["university"]
        seen = new_state.setdefault(name, {})
        first = name not in state

        def found(url: str, title: str) -> None:
            if not first:
                report.findings.append(Finding(name, url, title))

        if source.get("page"):
            try:
                html = fetch_html(source["page"])
            except Exception as exc:  # noqa: BLE001 — сайт вуза лежит: попробуем в следующий раз
                report.errors.append(f"{name}: {source['page']}: {exc}")
                html = None
            if html is not None:
                links = year_links(html, source["page"], year)
                old = set(seen.get("links", []))
                for url, title in links.items():
                    if url not in old:
                        found(url, title)
                seen["links"] = sorted(old | set(links))
                marker = source.get("marker")
                if marker:
                    present = marker.format(year=year) in re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" "))
                    if present and not seen.get("marker"):
                        found(source["page"], marker.format(year=year))
                    seen["marker"] = present or seen.get("marker", False)
        for template in source.get("probe", []):
            url = template.format(year=year)
            if url in seen.get("probes", []):
                continue
            try:
                fetch_bytes(url)
            except PageGone:
                continue
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{name}: {url}: {exc}")
                continue
            seen["probes"] = sorted(set(seen.get("probes", [])) | {url})
            found(url, f"документ на приём {year}")
        for url in source.get("hash", []):
            try:
                digest = hashlib.sha256(fetch_bytes(url)).hexdigest()
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{name}: {url}: {exc}")
                continue
            hashes = seen.setdefault("hash", {})
            if url in hashes and hashes[url] != digest:
                found(url, "документ об особых правах обновлён")
            hashes[url] = digest
    return report, new_state


def run_watch(storage, sources: list[dict], year: int, fetch_html, fetch_bytes) -> WatchReport:
    state = json.loads(storage.get_meta(STATE_KEY) or "{}")
    report, new_state = check_sources(sources, year, state, fetch_html, fetch_bytes)
    storage.set_meta(STATE_KEY, json.dumps(new_state, ensure_ascii=False))
    return report
