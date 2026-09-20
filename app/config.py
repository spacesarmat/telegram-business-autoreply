from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
    timezone_name: str
    web_admin_username: str
    web_admin_password: str


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("Не задан BOT_TOKEN")

    admin_ids = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
    if not admin_ids:
        raise RuntimeError("Не задан ADMIN_IDS")

    timezone_name = os.getenv("TZ", "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(
            f"Некорректный TZ: {timezone_name!r}. Используйте IANA-зону, например Europe/Moscow."
        ) from exc

    return Settings(
        bot_token=bot_token,
        admin_ids=admin_ids,
        database_path=os.getenv("DATABASE_PATH", "/data/bot.db").strip() or "/data/bot.db",
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        status_port=int(os.getenv("STATUS_PORT", "8080")),
        timezone_name=timezone_name,
        web_admin_username=os.getenv("WEB_ADMIN_USERNAME", "admin").strip() or "admin",
        web_admin_password=os.getenv("WEB_ADMIN_PASSWORD", "").strip(),
    )
