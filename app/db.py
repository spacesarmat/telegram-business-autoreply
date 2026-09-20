from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite


DEFAULT_SETTINGS = {
    "autoresponder_enabled": "1",
    "cooldown_hours": "168",
    "menu_columns": "1",
    "greeting": (
        "Здравствуйте! Спасибо за сообщение.\n\n"
        "Я отвечаю автоматически, если вы пишете впервые или после длительного перерыва. "
        "Выберите подходящий пункт ниже:"
    ),
}

DEFAULT_BUTTONS = [
    (
        "Написать пользователю",
        "Напишите, пожалуйста, ваш вопрос одним сообщением. Я увижу его в этом чате и отвечу, как только смогу.",
    ),
    (
        "Заказать мероприятие",
        "Отлично! Напишите дату, формат мероприятия, примерное количество гостей и ваши пожелания.",
    ),
    (
        "Аренда оборудования",
        "Укажите, пожалуйста, какое оборудование вас интересует, даты аренды и место использования.",
    ),
    (
        "Аренда Фабрики",
        "Напишите желаемую дату, длительность аренды, формат мероприятия и примерное количество гостей.",
    ),
    (
        "Другое",
        "Опишите ваш вопрос свободным текстом — я увижу сообщение и отвечу, как только смогу.",
    ),
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            await db.close()

    async def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with self.connection() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS menu_buttons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    response TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 100,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS contacts (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    last_inbound_at TEXT NOT NULL,
                    last_auto_reply_at TEXT,
                    auto_replies_sent INTEGER NOT NULL DEFAULT 0,
                    button_clicks INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS business_connections (
                    id TEXT PRIMARY KEY,
                    owner_user_id INTEGER NOT NULL,
                    user_chat_id INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    can_reply INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )

            for key, value in DEFAULT_SETTINGS.items():
                await db.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                    (key, value),
                )

            row = await (await db.execute("SELECT COUNT(*) AS c FROM menu_buttons")).fetchone()
            if row["c"] == 0:
                now = utc_now_iso()
                for position, (title, response) in enumerate(DEFAULT_BUTTONS, start=1):
                    await db.execute(
                        """
                        INSERT INTO menu_buttons(title, response, position, enabled, created_at, updated_at)
                        VALUES(?, ?, ?, 1, ?, ?)
                        """,
                        (title, response, position, now, now),
                    )
            await db.commit()

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        async with self.connection() as db:
            row = await (await db.execute("SELECT value FROM settings WHERE key=?", (key,))).fetchone()
            return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO settings(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )
            await db.commit()

    async def list_buttons(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM menu_buttons"
        args: tuple[Any, ...] = ()
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY position ASC, id ASC"
        async with self.connection() as db:
            rows = await (await db.execute(sql, args)).fetchall()
            return [dict(row) for row in rows]

    async def get_button(self, button_id: int) -> dict[str, Any] | None:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT * FROM menu_buttons WHERE id=?", (button_id,))
            ).fetchone()
            return dict(row) if row else None

    async def add_button(self, title: str, response: str) -> int:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM menu_buttons")
            ).fetchone()
            now = utc_now_iso()
            cur = await db.execute(
                """
                INSERT INTO menu_buttons(title, response, position, enabled, created_at, updated_at)
                VALUES(?, ?, ?, 1, ?, ?)
                """,
                (title, response, int(row["p"]), now, now),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def update_button_field(self, button_id: int, field: str, value: Any) -> None:
        allowed = {"title", "response", "position", "enabled"}
        if field not in allowed:
            raise ValueError("Unsupported field")
        async with self.connection() as db:
            await db.execute(
                f"UPDATE menu_buttons SET {field}=?, updated_at=? WHERE id=?",
                (value, utc_now_iso(), button_id),
            )
            await db.commit()

    async def delete_button(self, button_id: int) -> None:
        async with self.connection() as db:
            await db.execute("DELETE FROM menu_buttons WHERE id=?", (button_id,))
            await db.commit()

    async def get_contact(self, chat_id: int) -> dict[str, Any] | None:
        async with self.connection() as db:
            row = await (await db.execute("SELECT * FROM contacts WHERE chat_id=?", (chat_id,))).fetchone()
            return dict(row) if row else None

    async def upsert_contact(
        self,
        chat_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        inbound_at: str,
    ) -> None:
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO contacts(chat_id, username, first_name, last_name, last_inbound_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    last_inbound_at=excluded.last_inbound_at
                """,
                (chat_id, username, first_name, last_name, inbound_at),
            )
            await db.commit()

    async def mark_auto_reply(self, chat_id: int, at: str) -> None:
        async with self.connection() as db:
            await db.execute(
                """
                UPDATE contacts
                SET last_auto_reply_at=?, auto_replies_sent=auto_replies_sent+1
                WHERE chat_id=?
                """,
                (at, chat_id),
            )
            await db.commit()

    async def mark_button_click(self, chat_id: int, at: str) -> None:
        async with self.connection() as db:
            await db.execute(
                """
                UPDATE contacts
                SET last_inbound_at=?, button_clicks=button_clicks+1
                WHERE chat_id=?
                """,
                (at, chat_id),
            )
            await db.commit()

    async def save_business_connection(
        self,
        connection_id: str,
        owner_user_id: int,
        user_chat_id: int | None,
        enabled: bool,
        can_reply: bool,
    ) -> None:
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO business_connections(id, owner_user_id, user_chat_id, enabled, can_reply, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    owner_user_id=excluded.owner_user_id,
                    user_chat_id=excluded.user_chat_id,
                    enabled=excluded.enabled,
                    can_reply=excluded.can_reply,
                    updated_at=excluded.updated_at
                """,
                (
                    connection_id,
                    owner_user_id,
                    user_chat_id,
                    int(enabled),
                    int(can_reply),
                    utc_now_iso(),
                ),
            )
            await db.commit()

    async def get_business_connection(self, connection_id: str) -> dict[str, Any] | None:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT * FROM business_connections WHERE id=?", (connection_id,))
            ).fetchone()
            return dict(row) if row else None

    async def latest_business_connection(self) -> dict[str, Any] | None:
        async with self.connection() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM business_connections ORDER BY updated_at DESC LIMIT 1"
                )
            ).fetchone()
            return dict(row) if row else None

    async def stats(self) -> dict[str, int]:
        async with self.connection() as db:
            row = await (
                await db.execute(
                    """
                    SELECT
                        COUNT(*) AS contacts,
                        COALESCE(SUM(auto_replies_sent), 0) AS auto_replies,
                        COALESCE(SUM(button_clicks), 0) AS button_clicks
                    FROM contacts
                    """
                )
            ).fetchone()
            return {
                "contacts": int(row["contacts"]),
                "auto_replies": int(row["auto_replies"]),
                "button_clicks": int(row["button_clicks"]),
            }
