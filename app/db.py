from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite


DEFAULT_SETTINGS = {
    "autoresponder_enabled": "1",
    "cooldown_hours": "168",
    "menu_columns": "1",
    "menu_triggers": "/menu\nменю\nзаявка",
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
        "Отлично! Заполните короткую заявку — я получу её целиком после подтверждения.",
    ),
    (
        "Аренда оборудования",
        "Заполните короткую заявку на аренду оборудования — я получу её после подтверждения.",
    ),
    (
        "Аренда Фабрики",
        "Заполните короткую заявку на аренду Фабрики — я получу её после подтверждения.",
    ),
    (
        "Другое",
        "Опишите ваш вопрос в короткой форме — я получу его после подтверждения.",
    ),
]

DEFAULT_FORMS: list[dict[str, Any]] = [
    {
        "button_title": "Заказать мероприятие",
        "name": "Заказать мероприятие",
        "questions": [
            ("Дата", "На какую дату планируется мероприятие?", True, "date"),
            ("Формат", "Какой формат мероприятия планируется?", True),
            ("Количество гостей", "Сколько примерно будет гостей?", True),
            ("Место", "Где планируется мероприятие / какая площадка нужна?", False),
            ("Телефон", "Оставьте контактный телефон для связи.", True, "contact"),
            ("Комментарий", "Дополнительные пожелания или комментарий.", False),
        ],
    },
    {
        "button_title": "Аренда оборудования",
        "name": "Аренда оборудования",
        "questions": [
            ("Оборудование", "Какое оборудование вас интересует?", True),
            ("Даты аренды", "На какие даты нужна аренда?", True),
            ("Получение", "Доставка или самовывоз?", True),
            ("Адрес", "Укажите адрес доставки / место использования.", False),
            ("Телефон", "Оставьте контактный телефон для связи.", True, "contact"),
            ("Комментарий", "Дополнительные пожелания или комментарий.", False),
        ],
    },
    {
        "button_title": "Аренда Фабрики",
        "name": "Аренда Фабрики",
        "questions": [
            ("Дата", "На какую дату нужна аренда Фабрики?", True, "date"),
            ("Время", "Во сколько планируется начало?", True),
            ("Продолжительность", "На сколько часов нужна площадка?", True),
            ("Количество гостей", "Сколько примерно будет гостей?", True),
            ("Формат", "Какой формат мероприятия планируется?", True),
            ("Телефон", "Оставьте контактный телефон для связи.", True, "contact"),
            ("Комментарий", "Дополнительные пожелания или комментарий.", False),
        ],
    },
    {
        "button_title": "Другое",
        "name": "Другой вопрос",
        "questions": [
            ("Вопрос", "Опишите, пожалуйста, ваш вопрос.", True),
            ("Телефон", "Оставьте контактный телефон, если удобно.", False, "contact"),
        ],
    },
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_answers(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if v is not None}


class Database:
    def __init__(self, path: str):
        self.path = path

    @staticmethod
    def infer_question_input_type(label: str) -> str:
        normalized = " ".join(label.strip().casefold().split())
        if normalized == "дата":
            return "date"
        if normalized in {"телефон", "контакт", "контактный телефон"}:
            return "contact"
        return "text"

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
                    can_delete_all_messages INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS forms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS form_questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    form_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 100,
                    required INTEGER NOT NULL DEFAULT 1,
                    input_type TEXT NOT NULL DEFAULT 'text',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS button_forms (
                    button_id INTEGER PRIMARY KEY,
                    form_id INTEGER NOT NULL,
                    FOREIGN KEY(button_id) REFERENCES menu_buttons(id) ON DELETE CASCADE,
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS form_sessions (
                    chat_id INTEGER PRIMARY KEY,
                    business_connection_id TEXT NOT NULL,
                    form_id INTEGER NOT NULL,
                    form_message_id INTEGER,
                    keyboard_question_id INTEGER NOT NULL DEFAULT 0,
                    current_index INTEGER NOT NULL DEFAULT 0,
                    answers_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active',
                    user_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS form_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    form_id INTEGER,
                    form_name TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    answers_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_form_questions_form_position
                    ON form_questions(form_id, position, id);
                CREATE INDEX IF NOT EXISTS idx_form_submissions_created
                    ON form_submissions(created_at DESC);
                """
            )

            # Миграции для существующей базы без потери данных.
            bc_columns = {
                row["name"] for row in await (await db.execute("PRAGMA table_info(business_connections)")).fetchall()
            }
            if "can_delete_all_messages" not in bc_columns:
                await db.execute(
                    "ALTER TABLE business_connections ADD COLUMN can_delete_all_messages INTEGER NOT NULL DEFAULT 0"
                )

            session_columns = {
                row["name"] for row in await (await db.execute("PRAGMA table_info(form_sessions)")).fetchall()
            }
            if "form_message_id" not in session_columns:
                await db.execute("ALTER TABLE form_sessions ADD COLUMN form_message_id INTEGER")
            if "keyboard_question_id" not in session_columns:
                await db.execute(
                    "ALTER TABLE form_sessions ADD COLUMN keyboard_question_id INTEGER NOT NULL DEFAULT 0"
                )

            question_columns = {
                row["name"]
                for row in await (await db.execute("PRAGMA table_info(form_questions)")).fetchall()
            }
            if "input_type" not in question_columns:
                await db.execute(
                    "ALTER TABLE form_questions ADD COLUMN input_type TEXT NOT NULL DEFAULT 'text'"
                )
                await db.execute(
                    "UPDATE form_questions SET input_type='date' WHERE trim(label) IN ('Дата', 'дата', 'ДАТА')"
                )
                await db.execute(
                    "UPDATE form_questions SET input_type='contact' "
                    "WHERE trim(label) IN ('Телефон', 'телефон', 'ТЕЛЕФОН', 'Контакт', 'контакт', 'КОНТАКТ', 'Контактный телефон', 'контактный телефон')"
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

            seeded = await (
                await db.execute("SELECT value FROM settings WHERE key='forms_seeded_v1'")
            ).fetchone()
            if not seeded:
                await self._seed_default_forms(db)
                await db.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES('forms_seeded_v1', '1')"
                )
            await db.commit()

    async def _seed_default_forms(self, db: aiosqlite.Connection) -> None:
        now = utc_now_iso()
        for form_def in DEFAULT_FORMS:
            button = await (
                await db.execute(
                    "SELECT id FROM menu_buttons WHERE title=? ORDER BY id LIMIT 1",
                    (form_def["button_title"],),
                )
            ).fetchone()
            if not button:
                continue
            cur = await db.execute(
                "INSERT INTO forms(name, enabled, created_at, updated_at) VALUES(?, 1, ?, ?)",
                (form_def["name"], now, now),
            )
            form_id = int(cur.lastrowid)
            for position, question_def in enumerate(form_def["questions"], start=1):
                if len(question_def) == 4:
                    label, prompt, required, input_type = question_def
                else:
                    label, prompt, required = question_def
                    input_type = self.infer_question_input_type(str(label))
                await db.execute(
                    """
                    INSERT INTO form_questions(
                        form_id, label, prompt, position, required, input_type, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        form_id, label, prompt, position, int(required), str(input_type), now, now
                    ),
                )
            await db.execute(
                "INSERT OR REPLACE INTO button_forms(button_id, form_id) VALUES(?, ?)",
                (int(button["id"]), form_id),
            )

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
        can_delete_all_messages: bool = False,
    ) -> None:
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO business_connections(
                    id, owner_user_id, user_chat_id, enabled, can_reply,
                    can_delete_all_messages, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    owner_user_id=excluded.owner_user_id,
                    user_chat_id=excluded.user_chat_id,
                    enabled=excluded.enabled,
                    can_reply=excluded.can_reply,
                    can_delete_all_messages=excluded.can_delete_all_messages,
                    updated_at=excluded.updated_at
                """,
                (
                    connection_id,
                    owner_user_id,
                    user_chat_id,
                    int(enabled),
                    int(can_reply),
                    int(can_delete_all_messages),
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

    # ---------- Формы ----------

    async def list_forms(self) -> list[dict[str, Any]]:
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT f.*,
                           (SELECT COUNT(*) FROM form_questions q WHERE q.form_id=f.id) AS question_count,
                           (SELECT COUNT(*) FROM button_forms bf WHERE bf.form_id=f.id) AS button_count
                    FROM forms f
                    ORDER BY f.id ASC
                    """
                )
            ).fetchall()
            return [dict(row) for row in rows]

    async def get_form(self, form_id: int) -> dict[str, Any] | None:
        async with self.connection() as db:
            row = await (
                await db.execute(
                    """
                    SELECT f.*,
                           (SELECT COUNT(*) FROM form_questions q WHERE q.form_id=f.id) AS question_count,
                           (SELECT COUNT(*) FROM button_forms bf WHERE bf.form_id=f.id) AS button_count
                    FROM forms f WHERE f.id=?
                    """,
                    (form_id,),
                )
            ).fetchone()
            return dict(row) if row else None

    async def add_form(self, name: str) -> int:
        now = utc_now_iso()
        async with self.connection() as db:
            cur = await db.execute(
                "INSERT INTO forms(name, enabled, created_at, updated_at) VALUES(?, 1, ?, ?)",
                (name, now, now),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def update_form_field(self, form_id: int, field: str, value: Any) -> None:
        if field not in {"name", "enabled"}:
            raise ValueError("Unsupported form field")
        async with self.connection() as db:
            await db.execute(
                f"UPDATE forms SET {field}=?, updated_at=? WHERE id=?",
                (value, utc_now_iso(), form_id),
            )
            await db.commit()

    async def delete_form(self, form_id: int) -> None:
        async with self.connection() as db:
            await db.execute("DELETE FROM forms WHERE id=?", (form_id,))
            await db.commit()

    async def list_form_questions(self, form_id: int) -> list[dict[str, Any]]:
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM form_questions WHERE form_id=? ORDER BY position ASC, id ASC",
                    (form_id,),
                )
            ).fetchall()
            return [dict(row) for row in rows]

    async def get_form_question(self, question_id: int) -> dict[str, Any] | None:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT * FROM form_questions WHERE id=?", (question_id,))
            ).fetchone()
            return dict(row) if row else None

    async def add_form_question(
        self,
        form_id: int,
        label: str,
        prompt: str,
        required: bool = True,
        input_type: str | None = None,
    ) -> int:
        now = utc_now_iso()
        input_type = input_type or self.infer_question_input_type(label)
        if input_type not in {"text", "date", "contact"}:
            input_type = "text"
        async with self.connection() as db:
            row = await (
                await db.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 AS p FROM form_questions WHERE form_id=?",
                    (form_id,),
                )
            ).fetchone()
            cur = await db.execute(
                """
                INSERT INTO form_questions(
                    form_id, label, prompt, position, required, input_type, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    form_id, label, prompt, int(row["p"]), int(required), input_type, now, now
                ),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def update_form_question_field(self, question_id: int, field: str, value: Any) -> None:
        if field not in {"label", "prompt", "position", "required", "input_type"}:
            raise ValueError("Unsupported question field")
        async with self.connection() as db:
            await db.execute(
                f"UPDATE form_questions SET {field}=?, updated_at=? WHERE id=?",
                (value, utc_now_iso(), question_id),
            )
            await db.commit()

    async def delete_form_question(self, question_id: int) -> None:
        async with self.connection() as db:
            await db.execute("DELETE FROM form_questions WHERE id=?", (question_id,))
            await db.commit()

    async def list_form_button_ids(self, form_id: int) -> list[int]:
        async with self.connection() as db:
            rows = await (
                await db.execute("SELECT button_id FROM button_forms WHERE form_id=?", (form_id,))
            ).fetchall()
            return [int(row["button_id"]) for row in rows]

    async def set_button_form(self, button_id: int, form_id: int | None) -> None:
        async with self.connection() as db:
            if form_id is None:
                await db.execute("DELETE FROM button_forms WHERE button_id=?", (button_id,))
            else:
                await db.execute(
                    "INSERT OR REPLACE INTO button_forms(button_id, form_id) VALUES(?, ?)",
                    (button_id, form_id),
                )
            await db.commit()

    async def get_bound_form(self, button_id: int, enabled_only: bool = False) -> dict[str, Any] | None:
        sql = (
            "SELECT f.* FROM button_forms bf JOIN forms f ON f.id=bf.form_id "
            "WHERE bf.button_id=?"
        )
        if enabled_only:
            sql += " AND f.enabled=1"
        async with self.connection() as db:
            row = await (await db.execute(sql, (button_id,))).fetchone()
            return dict(row) if row else None

    async def start_form_session(
        self,
        chat_id: int,
        business_connection_id: str,
        form_id: int,
        user_id: int | None,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        form_message_id: int | None = None,
    ) -> None:
        now = utc_now_iso()
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO form_sessions(
                    chat_id, business_connection_id, form_id, form_message_id, keyboard_question_id, current_index, answers_json, status,
                    user_id, username, first_name, last_name, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 0, 0, '{}', 'active', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    business_connection_id=excluded.business_connection_id,
                    form_id=excluded.form_id,
                    form_message_id=excluded.form_message_id,
                    keyboard_question_id=0,
                    current_index=0,
                    answers_json='{}',
                    status='active',
                    user_id=excluded.user_id,
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at
                """,
                (
                    chat_id,
                    business_connection_id,
                    form_id,
                    form_message_id,
                    user_id,
                    username,
                    first_name,
                    last_name,
                    now,
                    now,
                ),
            )
            await db.commit()

    async def get_form_session(self, chat_id: int) -> dict[str, Any] | None:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT * FROM form_sessions WHERE chat_id=?", (chat_id,))
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            result["answers"] = _decode_answers(result.get("answers_json"))
            return result

    async def update_form_session(
        self,
        chat_id: int,
        *,
        current_index: int | None = None,
        answers: dict[str, str] | None = None,
        status: str | None = None,
        business_connection_id: str | None = None,
        form_message_id: int | None = None,
        keyboard_question_id: int | None = None,
    ) -> None:
        fields: list[str] = []
        values: list[Any] = []
        if current_index is not None:
            fields.append("current_index=?")
            values.append(current_index)
        if answers is not None:
            fields.append("answers_json=?")
            values.append(json.dumps(answers, ensure_ascii=False))
        if status is not None:
            fields.append("status=?")
            values.append(status)
        if business_connection_id is not None:
            fields.append("business_connection_id=?")
            values.append(business_connection_id)
        if form_message_id is not None:
            fields.append("form_message_id=?")
            values.append(form_message_id)
        if keyboard_question_id is not None:
            fields.append("keyboard_question_id=?")
            values.append(keyboard_question_id)
        fields.append("updated_at=?")
        values.append(utc_now_iso())
        values.append(chat_id)
        async with self.connection() as db:
            await db.execute(
                f"UPDATE form_sessions SET {', '.join(fields)} WHERE chat_id=?",
                tuple(values),
            )
            await db.commit()

    async def delete_form_session(self, chat_id: int) -> None:
        async with self.connection() as db:
            await db.execute("DELETE FROM form_sessions WHERE chat_id=?", (chat_id,))
            await db.commit()

    async def create_form_submission(self, session: dict[str, Any], form_name: str) -> int:
        now = utc_now_iso()
        answers = session.get("answers") or _decode_answers(session.get("answers_json"))
        async with self.connection() as db:
            cur = await db.execute(
                """
                INSERT INTO form_submissions(
                    form_id, form_name, chat_id, user_id, username, first_name, last_name,
                    answers_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.get("form_id"),
                    form_name,
                    session["chat_id"],
                    session.get("user_id"),
                    session.get("username"),
                    session.get("first_name"),
                    session.get("last_name"),
                    json.dumps(answers, ensure_ascii=False),
                    now,
                ),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def stats(self) -> dict[str, int]:
        async with self.connection() as db:
            contact = await (
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
            forms = await (
                await db.execute(
                    "SELECT COUNT(*) AS submissions FROM form_submissions"
                )
            ).fetchone()
            return {
                "contacts": int(contact["contacts"]),
                "auto_replies": int(contact["auto_replies"]),
                "button_clicks": int(contact["button_clicks"]),
                "submissions": int(forms["submissions"]),
            }
