from __future__ import annotations

import os
from dataclasses import dataclass


def _parse_admin_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.add(int(item))
        except ValueError as exc:
            raise RuntimeError(f"Некорректный ADMIN_IDS: {item!r}") from exc
    return result


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: set[int]
    database_path: str
    log_level: str
    status_port: int


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("Не задан BOT_TOKEN")

    admin_ids = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
    if not admin_ids:
        raise RuntimeError("Не задан ADMIN_IDS")

    return Settings(
        bot_token=bot_token,
        admin_ids=admin_ids,
        database_path=os.getenv("DATABASE_PATH", "/data/bot.db").strip() or "/data/bot.db",
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        status_port=int(os.getenv("STATUS_PORT", "8080")),
    )
