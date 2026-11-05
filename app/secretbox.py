"""Шифрование доступов к кабинетам олимпиад (логин и пароль клиента).

Ключ — CRM_KEY в .env (Fernet: 32 случайных байта в base64url), в базе лежит только шифротекст.
Без ключа приём доступов выключен: хранить пароли открытым текстом нельзя.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: str):
        self._f = Fernet(key.encode()) if key else None

    @property
    def enabled(self) -> bool:
        return self._f is not None

    def seal(self, text: str) -> str:
        if self._f is None:
            raise RuntimeError("CRM_KEY не задан")
        return self._f.encrypt(text.encode()).decode()

    def open(self, token: str) -> str:
        if self._f is None:
            raise RuntimeError("CRM_KEY не задан")
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken:
            return "(не расшифровать: сменился CRM_KEY)"

    @staticmethod
    def new_key() -> str:
        return Fernet.generate_key().decode()
