"""Диагностика связи с Telegram: python -m app.cli bot check

Проверяет по отдельности IPv4 и IPv6, установку TLS-соединения и несколько запросов
подряд с новыми соединениями — именно так бот отправляет ответы. Только стандартная
библиотека, чтобы проверка работала даже при проблемах с зависимостями.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request

HOST = "api.telegram.org"
TIMEOUT = 8


def _addresses(family: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(HOST, 443, family, socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    seen: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.append(ip)
    return seen[:2]


def _tls_connect(ip: str, family: int) -> tuple[bool, str]:
    """TCP + TLS до конкретного адреса. Возвращает (успех, описание)."""
    started = time.monotonic()
    try:
        with socket.create_connection((ip, 443), timeout=TIMEOUT) as raw:
            context = ssl.create_default_context()
            with context.wrap_socket(raw, server_hostname=HOST):
                pass
        return True, f"{(time.monotonic() - started) * 1000:.0f} мс"
    except (socket.timeout, TimeoutError):
        return False, f"нет ответа за {TIMEOUT} с"
    except OSError as exc:
        return False, f"ошибка: {exc}"


def _api_call(token: str, ip: str) -> tuple[bool, str]:
    """getMe через конкретный IPv4-адрес, каждый раз новым соединением."""
    started = time.monotonic()
    context = ssl.create_default_context()
    try:
        with socket.create_connection((ip, 443), timeout=TIMEOUT) as raw:
            with context.wrap_socket(raw, server_hostname=HOST) as tls:
                tls.settimeout(TIMEOUT)
                request = (f"GET /bot{token}/getMe HTTP/1.1\r\nHost: {HOST}\r\n"
                           "Connection: close\r\n\r\n").encode()
                tls.sendall(request)
                data = b""
                while chunk := tls.recv(4096):
                    data += chunk
        body = data.split(b"\r\n\r\n", 1)[-1]
        ok = json.loads(body.decode("utf-8", "replace")).get("ok") is True
        elapsed = f"{(time.monotonic() - started) * 1000:.0f} мс"
        return ok, elapsed if ok else f"{elapsed}, Telegram ответил ошибкой (проверьте токен)"
    except (socket.timeout, TimeoutError):
        return False, f"нет ответа за {TIMEOUT} с"
    except (OSError, ValueError) as exc:
        return False, f"ошибка: {exc}"


def _proxy_call(token: str, proxy: str) -> tuple[bool, str]:
    if not proxy.startswith(("http://", "https://")):
        return False, "проверка умеет только http-прокси; socks проверьте запуском бота"
    started = time.monotonic()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy}))
    try:
        with opener.open(f"https://{HOST}/bot{token}/getMe", timeout=TIMEOUT) as resp:
            ok = json.loads(resp.read().decode()).get("ok") is True
        return ok, f"{(time.monotonic() - started) * 1000:.0f} мс"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"ошибка: {exc}"


def run_check(token: str, proxy: str = "", attempts: int = 5) -> int:
    print(f"Проверяю связь с {HOST}\n")
    results: dict[str, list[bool]] = {"IPv4": [], "IPv6": []}
    for label, family in (("IPv4", socket.AF_INET), ("IPv6", socket.AF_INET6)):
        ips = _addresses(family)
        if not ips:
            print(f"{label}: адресов нет")
            continue
        for ip in ips:
            ok, info = _tls_connect(ip, family)
            results[label].append(ok)
            print(f"{label} {ip}: {'✓' if ok else '✗'} {info}")

    api_ok: list[bool] = []
    ipv4 = _addresses(socket.AF_INET)
    if token and ipv4:
        print(f"\n{attempts} запросов getMe подряд, каждый новым соединением (как ответы бота):")
        for i in range(attempts):
            ok, info = _api_call(token, ipv4[i % len(ipv4)])
            api_ok.append(ok)
            print(f"  {i + 1}. {'✓' if ok else '✗'} {info}")

    proxy_ok = None
    if token and proxy:
        proxy_ok, info = _proxy_call(token, proxy)
        print(f"\nЧерез прокси {proxy.split('@')[-1]}: {'✓' if proxy_ok else '✗'} {info}")

    print("\nИтог:")
    if not _addresses(socket.AF_INET) and not _addresses(socket.AF_INET6):
        print(f"• Не удалось узнать адрес {HOST} — нет интернета или не работает DNS.")
        return 1
    v4_ok = any(results["IPv4"]) and (not api_ok or all(api_ok))
    v6_bad = results["IPv6"] and not any(results["IPv6"])
    if v4_ok and v6_bad:
        print("• IPv6 до Telegram не работает, IPv4 работает. Бот по умолчанию ходит по IPv4 "
              "(TELEGRAM_FORCE_IPV4=1) — этого должно хватить.")
    elif v4_ok:
        print("• Связь с Telegram в порядке.")
    elif api_ok and any(api_ok):
        print("• Соединения с Telegram устанавливаются через раз — похоже на замедление у провайдера. "
              "Нужен прокси: TELEGRAM_PROXY в .env (см. README).")
    else:
        print("• Telegram недоступен напрямую. Нужен прокси: TELEGRAM_PROXY в .env (см. README) "
              "или запуск бота на сервере за пределами этой сети.")
    if proxy_ok is not None:
        print("• Прокси работает." if proxy_ok else "• Прокси не отвечает — проверьте адрес, логин и пароль.")
    if not token:
        print("• Токен не задан, поэтому запросы к API не проверялись.")
    return 0 if (v4_ok or proxy_ok) else 1
