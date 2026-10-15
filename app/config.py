"""Настройки из переменных окружения (или файла .env в корне проекта)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Минимальная загрузка .env без внешних зависимостей. Переменные окружения важнее файла."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get("DATABASE_URL", "postgresql://olymp:olymp@localhost:5432/olymp")
    user_agent: str = os.environ.get(
        "USER_AGENT", "OlympNavigatorBot/0.1 (+https://github.com/your-name/olymp-navigator)"
    )
    request_delay: float = float(os.environ.get("REQUEST_DELAY", "3"))
    request_timeout: float = float(os.environ.get("REQUEST_TIMEOUT", "30"))
    data_dir: Path = Path(os.environ.get("DATA_DIR", PROJECT_ROOT / "data"))


settings = Settings()
