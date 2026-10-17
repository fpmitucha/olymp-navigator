"""Аккуратная загрузка страниц: свой User-Agent, таймауты, повторы, пауза между запросами."""

from __future__ import annotations

import time

import requests

from app.config import settings

_session = requests.Session()
_session.headers.update({
    "User-Agent": settings.user_agent,
    "Accept-Language": "ru,en;q=0.5",
})
_last_request_at = 0.0


def fetch_html(url: str, *, retries: int = 3) -> str:
    """Скачивает страницу. Между запросами выдерживает паузу settings.request_delay секунд."""
    response = _get(url, retries)
    response.encoding = response.encoding or response.apparent_encoding or "utf-8"
    return response.text


def fetch_bytes(url: str, *, retries: int = 3) -> bytes:
    """Скачивает файл (xlsx, pdf) целиком."""
    return _get(url, retries).content


class PageGone(RuntimeError):
    """Страницы нет (404 / 410) — повторять запрос бессмысленно."""


def _get(url: str, retries: int) -> requests.Response:
    global _last_request_at
    error: Exception | None = None
    for attempt in range(1, retries + 1):
        wait = settings.request_delay - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            response = _session.get(url, timeout=settings.request_timeout)
            _last_request_at = time.monotonic()
            if response.status_code in (404, 410):
                raise PageGone(f"Страницы нет ({response.status_code}): {url}")
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            _last_request_at = time.monotonic()
            error = exc
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Не удалось загрузить {url}: {error}") from error
