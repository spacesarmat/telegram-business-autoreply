from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout

from .db import Database, utc_now_iso


class OperationsService:
    """Operational layer for multi-venue booking, holds, waitlist and CRM workflow."""

    def __init__(self, db: Database, timezone: ZoneInfo) -> None:
        self.db = db
        self.timezone = timezone

    async def init(self) -> None:
        now = utc_now_iso()
        async with self.db.connection() as conn:
            await conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS venues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    slug TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    position INTEGER NOT NULL DEFAULT 100,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS form_venues (
                    form_id INTEGER NOT NULL,
                    venue_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(form_id, venue_id),
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE CASCADE,
                    FOREIGN KEY(venue_id) REFERENCES venues(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS venue_pricing (
                    form_id INTEGER NOT NULL,
                    venue_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    base_amount INTEGER NOT NULL DEFAULT 0,
                    base_description TEXT NOT NULL DEFAULT '',
                    included_hours INTEGER NOT NULL DEFAULT 0,
                    extra_hour_amount INTEGER NOT NULL DEFAULT 0,
                    buffer_before_minutes INTEGER NOT NULL DEFAULT 0,
                    buffer_after_minutes INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(form_id, venue_id),
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE CASCADE,
                    FOREIGN KEY(venue_id) REFERENCES venues(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS venue_booking_rules (
                    form_id INTEGER NOT NULL,
                    venue_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    min_duration_minutes INTEGER NOT NULL DEFAULT 0,
                    min_lead_hours INTEGER NOT NULL DEFAULT 0,
                    max_advance_days INTEGER NOT NULL DEFAULT 365,
                    closed_weekdays_json TEXT NOT NULL DEFAULT '[]',
                    day_hours_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(form_id, venue_id),
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE CASCADE,
                    FOREIGN KEY(venue_id) REFERENCES venues(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS venue_recurring_blocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    form_id INTEGER NOT NULL,
                    venue_id INTEGER NOT NULL,
                    weekday INTEGER NOT NULL,
                    start_time TEXT,
                    end_time TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE CASCADE,
                    FOREIGN KEY(venue_id) REFERENCES venues(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_venue_recurring_blocks
                    ON venue_recurring_blocks(form_id, venue_id, weekday, enabled);
                CREATE TABLE IF NOT EXISTS slot_holds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT NOT NULL UNIQUE,
                    submission_token TEXT,
                    form_id INTEGER,
                    venue_id INTEGER,
                    chat_id INTEGER,
                    date_iso TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE CASCADE,
                    FOREIGN KEY(venue_id) REFERENCES venues(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_slot_holds_active
                    ON slot_holds(venue_id, date_iso, status, expires_at);
                CREATE TABLE IF NOT EXISTS waitlist_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    form_id INTEGER,
                    venue_id INTEGER,
                    chat_id INTEGER NOT NULL,
                    business_connection_id TEXT,
                    user_id INTEGER,
                    username TEXT,
                    date_iso TEXT NOT NULL,
                    start_time TEXT,
                    end_time TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'waiting',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE SET NULL,
                    FOREIGN KEY(venue_id) REFERENCES venues(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_waitlist_lookup
                    ON waitlist_entries(venue_id, date_iso, status);
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    venue_id INTEGER,
                    name TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    position INTEGER NOT NULL DEFAULT 100,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(venue_id) REFERENCES venues(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS resource_allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_id INTEGER NOT NULL,
                    resource_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(submission_id, resource_id),
                    FOREIGN KEY(submission_id) REFERENCES form_submissions(id) ON DELETE CASCADE,
                    FOREIGN KEY(resource_id) REFERENCES resources(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS service_packages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    form_id INTEGER,
                    venue_id INTEGER,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    amount INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    position INTEGER NOT NULL DEFAULT 100,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE CASCADE,
                    FOREIGN KEY(venue_id) REFERENCES venues(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS submission_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    assignee TEXT NOT NULL DEFAULT '',
                    due_at TEXT,
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(submission_id) REFERENCES form_submissions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_submission_tasks
                    ON submission_tasks(submission_id, status, due_at);
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS public_booking_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    venue_id INTEGER,
                    name TEXT NOT NULL,
                    contact TEXT NOT NULL,
                    date_iso TEXT NOT NULL,
                    start_time TEXT,
                    end_time TEXT,
                    guests TEXT NOT NULL DEFAULT '',
                    comment TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(venue_id) REFERENCES venues(id) ON DELETE SET NULL
                );
                """
            )

            # Backward-compatible columns on existing core tables.
            sub_cols = {str(r["name"]) for r in await (await conn.execute("PRAGMA table_info(form_submissions)")).fetchall()}
            for name, ddl in {
                "venue_id": "INTEGER",
                "manager": "TEXT NOT NULL DEFAULT ''",
                "archived": "INTEGER NOT NULL DEFAULT 0",
                "public_token": "TEXT",
                "deposit_amount": "INTEGER NOT NULL DEFAULT 0",
                "deposit_status": "TEXT NOT NULL DEFAULT 'none'",
            }.items():
                if name not in sub_cols:
                    await conn.execute(f"ALTER TABLE form_submissions ADD COLUMN {name} {ddl}")
            av_cols = {str(r["name"]) for r in await (await conn.execute("PRAGMA table_info(availability_blocks)")).fetchall()}
            if "venue_id" not in av_cols:
                await conn.execute("ALTER TABLE availability_blocks ADD COLUMN venue_id INTEGER")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_availability_venue_date ON availability_blocks(venue_id,date_iso,start_time,end_time)")
            await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_submission_public_token ON form_submissions(public_token) WHERE public_token IS NOT NULL")
            await conn.commit()

        # Seed two halls for existing installations.
        if not await self.list_venues(include_disabled=True):
            await self.create_venue("Зал 1", "Основной зал")
            await self.create_venue("Зал 2", "Второй зал")

        # Bind all current forms to all venues by default, then create a venue question
        # in the factory-rental form if it does not exist yet.
        venues = await self.list_venues()
        async with self.db.connection() as conn:
            forms = await (await conn.execute("SELECT id,name FROM forms")).fetchall()
            for form in forms:
                for venue in venues:
                    await conn.execute(
                        "INSERT OR IGNORE INTO form_venues(form_id,venue_id,enabled) VALUES(?,?,1)",
                        (int(form["id"]), int(venue["id"])),
                    )
                if "аренд" in str(form["name"]).casefold() and "фабрик" in str(form["name"]).casefold():
                    existing = await (await conn.execute(
                        "SELECT id FROM form_questions WHERE form_id=? AND input_type='venue' LIMIT 1",
                        (int(form["id"]),),
                    )).fetchone()
                    if not existing:
                        minpos = await (await conn.execute(
                            "SELECT MIN(position) p FROM form_questions WHERE form_id=?", (int(form["id"]),)
                        )).fetchone()
                        pos = max(1, int(minpos["p"] or 20) - 5)
                        await conn.execute(
                            "INSERT INTO form_questions(form_id,label,prompt,position,required,input_type,choice_options_json,created_at,updated_at) VALUES(?,?,?,?,1,'venue','[]',?,?)",
                            (int(form["id"]), "Зал", "Выберите зал", pos, now, now),
                        )
            await conn.execute(
                "UPDATE form_submissions SET public_token=lower(hex(randomblob(16))) WHERE public_token IS NULL OR public_token=''"
            )
            await conn.commit()

        defaults = {
            "slot_hold_minutes": "15",
            "public_booking_enabled": "0",
            "miniapp_enabled": "0",
            "miniapp_url": "",
            "api_enabled": "0",
            "api_token": secrets.token_urlsafe(32),
            "webhook_url": "",
            "webhook_secret": secrets.token_urlsafe(24),
            "archive_after_days": "365",
            "payment_provider": "off",
            "payment_provider_key": "",
            "yookassa_shop_id": "",
            "yookassa_secret_key": "",
            "payment_return_url": "",
        }
        for key, value in defaults.items():
            if await self.db.get_setting(key, None) is None:
                await self.db.set_setting(key, value)

    @staticmethod
    def _slug(name: str) -> str:
        base = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
        base = "-".join(x for x in base.split("-") if x)
        return base[:60] or secrets.token_hex(4)

    async def list_venues(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM venues"
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY position,id"
        async with self.db.connection() as conn:
            rows = await (await conn.execute(sql)).fetchall()
        return [dict(r) for r in rows]

    async def get_venue(self, venue_id: int) -> dict[str, Any] | None:
        async with self.db.connection() as conn:
            row = await (await conn.execute("SELECT * FROM venues WHERE id=?", (venue_id,))).fetchone()
        return dict(row) if row else None

    async def create_venue(self, name: str, description: str = "") -> int:
        now = utc_now_iso(); slug = self._slug(name)
        async with self.db.connection() as conn:
            suffix = 1; candidate = slug
            while await (await conn.execute("SELECT 1 FROM venues WHERE slug=?", (candidate,))).fetchone():
                suffix += 1; candidate = f"{slug}-{suffix}"
            cur = await conn.execute(
                "INSERT INTO venues(name,slug,description,enabled,position,created_at,updated_at) VALUES(?,?,?,1,100,?,?)",
                (name[:120], candidate, description[:2000], now, now),
            )
            await conn.commit(); return int(cur.lastrowid)

    async def update_venue(
        self, venue_id: int, *, name: str, description: str, enabled: bool,
        capacity: int | None = None, working_hours_json: str | None = None,
        buffer_before_minutes: int | None = None, buffer_after_minutes: int | None = None,
        equipment_description: str | None = None,
    ) -> None:
        values: list[Any] = [name[:120], description[:2000], 1 if enabled else 0, utc_now_iso()]
        assignments = ["name=?", "description=?", "enabled=?", "updated_at=?"]
        extras = {
            "capacity": None if capacity is None else max(0, int(capacity)),
            "working_hours_json": working_hours_json,
            "buffer_before_minutes": None if buffer_before_minutes is None else max(0, int(buffer_before_minutes)),
            "buffer_after_minutes": None if buffer_after_minutes is None else max(0, int(buffer_after_minutes)),
            "equipment_description": equipment_description,
        }
        for field, value in extras.items():
            if value is not None:
                assignments.append(f"{field}=?")
                values.append(value)
        values.append(venue_id)
        async with self.db.connection() as conn:
            await conn.execute(
                f"UPDATE venues SET {','.join(assignments)} WHERE id=?", tuple(values),
            )
            await conn.commit()

    async def venues_for_form(self, form_id: int) -> list[dict[str, Any]]:
        async with self.db.connection() as conn:
            rows = await (await conn.execute(
                """SELECT v.* FROM venues v JOIN form_venues fv ON fv.venue_id=v.id
                   WHERE fv.form_id=? AND fv.enabled=1 AND v.enabled=1 ORDER BY v.position,v.id""",
                (form_id,),
            )).fetchall()
        return [dict(r) for r in rows]

    async def resolve_venue(self, form_id: int, answer: str | None) -> dict[str, Any] | None:
        raw = str(answer or "").strip().casefold()
        if not raw:
            return None
        for venue in await self.venues_for_form(form_id):
            if str(venue["name"]).strip().casefold() == raw:
                return venue
        return None

    async def get_effective_pricing(self, form_id: int, venue_id: int | None) -> dict[str, Any]:
        base = await self.db.get_form_pricing(form_id)
        if not venue_id:
            return base
        async with self.db.connection() as conn:
            row = await (await conn.execute("SELECT * FROM venue_pricing WHERE form_id=? AND venue_id=?", (form_id, venue_id))).fetchone()
        if not row or not int(row["enabled"] or 0):
            return base
        item=dict(base); override=dict(row)
        for key in ("enabled","base_amount","base_description","included_hours","extra_hour_amount","buffer_before_minutes","buffer_after_minutes"):
            item[key]=override.get(key)
        item["venue_override"]=True
        return item

    async def save_venue_pricing(self, form_id: int, venue_id: int, values: dict[str, Any]) -> None:
        now=utc_now_iso()
        defaults={"enabled":0,"base_amount":0,"base_description":"","included_hours":0,"extra_hour_amount":0,"buffer_before_minutes":0,"buffer_after_minutes":0}
        defaults.update({k:v for k,v in values.items() if k in defaults})
        async with self.db.connection() as conn:
            await conn.execute("""INSERT INTO venue_pricing(form_id,venue_id,enabled,base_amount,base_description,included_hours,extra_hour_amount,buffer_before_minutes,buffer_after_minutes,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(form_id,venue_id) DO UPDATE SET enabled=excluded.enabled,base_amount=excluded.base_amount,base_description=excluded.base_description,included_hours=excluded.included_hours,extra_hour_amount=excluded.extra_hour_amount,buffer_before_minutes=excluded.buffer_before_minutes,buffer_after_minutes=excluded.buffer_after_minutes,updated_at=excluded.updated_at""",
                (form_id,venue_id,int(bool(defaults["enabled"])),max(0,int(defaults["base_amount"] or 0)),str(defaults["base_description"] or "")[:1500],max(0,int(defaults["included_hours"] or 0)),max(0,int(defaults["extra_hour_amount"] or 0)),max(0,int(defaults["buffer_before_minutes"] or 0)),max(0,int(defaults["buffer_after_minutes"] or 0)),now))
            await conn.commit()

    async def list_venue_pricing(self) -> list[dict[str,Any]]:
        async with self.db.connection() as conn:
            rows=await (await conn.execute("SELECT vp.*,v.name venue_name,f.name form_name FROM venue_pricing vp JOIN venues v ON v.id=vp.venue_id JOIN forms f ON f.id=vp.form_id ORDER BY f.name,v.position,v.id")).fetchall()
        return [dict(r) for r in rows]

    async def get_venue_rule(self, form_id: int, venue_id: int | None) -> dict[str, Any]:
        default = {
            "form_id": form_id, "venue_id": venue_id, "enabled": 0,
            "min_duration_minutes": 0, "min_lead_hours": 0,
            "max_advance_days": 365, "closed_weekdays": [], "day_hours": {},
        }
        if not venue_id:
            return default
        async with self.db.connection() as conn:
            row = await (await conn.execute(
                "SELECT * FROM venue_booking_rules WHERE form_id=? AND venue_id=?",
                (form_id, venue_id),
            )).fetchone()
        if not row:
            return default
        item = dict(row)
        try:
            item["closed_weekdays"] = [int(x) for x in json.loads(item.get("closed_weekdays_json") or "[]")]
        except Exception:
            item["closed_weekdays"] = []
        try:
            raw_hours = json.loads(item.get("day_hours_json") or "{}")
            item["day_hours"] = raw_hours if isinstance(raw_hours, dict) else {}
        except Exception:
            item["day_hours"] = {}
        return item

    async def save_venue_rule(self, form_id: int, venue_id: int, values: dict[str, Any]) -> None:
        closed = values.get("closed_weekdays") or []
        closed = sorted({int(x) for x in closed if str(x).lstrip("-").isdigit() and 0 <= int(x) <= 6})
        hours = values.get("day_hours") or {}
        if not isinstance(hours, dict):
            hours = {}
        now = utc_now_iso()
        async with self.db.connection() as conn:
            await conn.execute(
                """INSERT INTO venue_booking_rules(
                    form_id,venue_id,enabled,min_duration_minutes,min_lead_hours,max_advance_days,
                    closed_weekdays_json,day_hours_json,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(form_id,venue_id) DO UPDATE SET
                    enabled=excluded.enabled,min_duration_minutes=excluded.min_duration_minutes,
                    min_lead_hours=excluded.min_lead_hours,max_advance_days=excluded.max_advance_days,
                    closed_weekdays_json=excluded.closed_weekdays_json,day_hours_json=excluded.day_hours_json,
                    updated_at=excluded.updated_at""",
                (
                    form_id, venue_id, 1 if values.get("enabled") else 0,
                    max(0, int(values.get("min_duration_minutes") or 0)),
                    max(0, int(values.get("min_lead_hours") or 0)),
                    max(0, int(values.get("max_advance_days") or 0)),
                    json.dumps(closed, ensure_ascii=False),
                    json.dumps(hours, ensure_ascii=False), now,
                ),
            )
            await conn.commit()

    async def list_venue_rules(self) -> list[dict[str, Any]]:
        async with self.db.connection() as conn:
            rows = await (await conn.execute(
                """SELECT r.*, f.name form_name, v.name venue_name
                   FROM venue_booking_rules r
                   JOIN forms f ON f.id=r.form_id JOIN venues v ON v.id=r.venue_id
                   ORDER BY f.name,v.position,v.id"""
            )).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try: item["closed_weekdays"] = json.loads(item.get("closed_weekdays_json") or "[]")
            except Exception: item["closed_weekdays"] = []
            result.append(item)
        return result

    async def list_venue_recurring_blocks(self, form_id: int, venue_id: int) -> list[dict[str, Any]]:
        async with self.db.connection() as conn:
            rows = await (await conn.execute(
                """SELECT * FROM venue_recurring_blocks
                   WHERE form_id=? AND venue_id=? ORDER BY weekday,COALESCE(start_time,''),id""",
                (form_id, venue_id),
            )).fetchall()
        return [dict(r) for r in rows]

    async def list_all_venue_recurring_blocks(self) -> list[dict[str, Any]]:
        async with self.db.connection() as conn:
            rows = await (await conn.execute(
                """SELECT b.*,f.name form_name,v.name venue_name
                   FROM venue_recurring_blocks b JOIN forms f ON f.id=b.form_id
                   JOIN venues v ON v.id=b.venue_id
                   ORDER BY f.name,v.position,b.weekday,COALESCE(b.start_time,''),b.id"""
            )).fetchall()
        return [dict(r) for r in rows]

    async def add_venue_recurring_block(
        self, *, form_id: int, venue_id: int, weekday: int,
        start_time: str | None, end_time: str | None, note: str,
    ) -> int:
        if not 0 <= int(weekday) <= 6:
            raise ValueError("Некорректный день недели")
        now = utc_now_iso()
        async with self.db.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO venue_recurring_blocks(
                    form_id,venue_id,weekday,start_time,end_time,note,enabled,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,1,?,?)""",
                (form_id, venue_id, int(weekday), start_time, end_time, str(note or "")[:200], now, now),
            )
            await conn.commit()
            return int(cur.lastrowid)

    async def delete_venue_recurring_block(self, block_id: int) -> None:
        async with self.db.connection() as conn:
            await conn.execute("DELETE FROM venue_recurring_blocks WHERE id=?", (block_id,))
            await conn.commit()

    @staticmethod
    def _time_minutes(value: str | None) -> int | None:
        if not value:
            return None
        if value == "24:00":
            return 1440
        try:
            h, m = str(value).split(":", 1)
            h_i, m_i = int(h), int(m)
        except Exception:
            return None
        if not (0 <= h_i <= 23 and 0 <= m_i <= 59):
            return None
        return h_i * 60 + m_i

    async def venue_rule_violation(self, form_id: int, venue_id: int | None, interval: dict | None) -> str | None:
        if not interval or not venue_id:
            return None
        rule = await self.get_venue_rule(form_id, venue_id)
        if not int(rule.get("enabled") or 0):
            return await self.venue_recurring_violation(form_id, venue_id, interval.get("segments") or [])
        try:
            start_date = datetime.strptime(str(interval.get("date_iso") or ""), "%Y-%m-%d").date()
        except ValueError:
            return None
        duration = max(0, int(interval.get("duration_minutes") or 0))
        min_duration = max(0, int(rule.get("min_duration_minutes") or 0))
        if min_duration and duration < min_duration:
            h, m = divmod(min_duration, 60)
            label = f"{h} ч" + (f" {m} мин" if m else "")
            return f"Для выбранного зала минимальная длительность — {label}."
        start_minutes = self._time_minutes(str(interval.get("start_time") or ""))
        if start_minutes is not None:
            start_local = datetime.combine(start_date, datetime.min.time(), self.timezone) + timedelta(minutes=start_minutes)
            lead = max(0, int(rule.get("min_lead_hours") or 0))
            if lead and start_local < datetime.now(self.timezone) + timedelta(hours=lead):
                return f"Для выбранного зала бронирование возможно минимум за {lead} ч до начала."
            horizon = max(0, int(rule.get("max_advance_days") or 0))
            if horizon and start_date > datetime.now(self.timezone).date() + timedelta(days=horizon):
                return f"Для выбранного зала бронь доступна максимум на {horizon} дней вперёд."
        closed = {int(x) for x in rule.get("closed_weekdays") or []}
        if start_date.weekday() in closed:
            return "Выбранный зал закрыт в этот день недели."
        hours = rule.get("day_hours") or {}
        for seg_date, seg_start, seg_end in interval.get("segments") or []:
            try: wd = datetime.strptime(seg_date, "%Y-%m-%d").date().weekday()
            except ValueError: continue
            window = hours.get(str(wd)) or hours.get(wd)
            if not isinstance(window, (list, tuple)) or len(window) != 2:
                continue
            s=self._time_minutes(seg_start); e=self._time_minutes(seg_end)
            a=self._time_minutes(str(window[0])); b=self._time_minutes(str(window[1]))
            if None not in {s,e,a,b} and not (int(s) >= int(a) and int(e) <= int(b)):
                return f"Для выбранного зала в этот день доступное время: {window[0]}–{window[1]}."
        return await self.venue_recurring_violation(form_id, venue_id, interval.get("segments") or [])

    async def venue_recurring_violation(
        self, form_id: int, venue_id: int | None, segments: list[tuple[str, str, str]]
    ) -> str | None:
        if not venue_id:
            return None
        blocks = await self.list_venue_recurring_blocks(form_id, venue_id)
        for seg_date, seg_start, seg_end in segments:
            try: weekday = datetime.strptime(seg_date, "%Y-%m-%d").date().weekday()
            except ValueError: continue
            for block in blocks:
                if not int(block.get("enabled") or 0) or int(block.get("weekday") if block.get("weekday") is not None else -1) != weekday:
                    continue
                bs, be = block.get("start_time"), block.get("end_time")
                if not bs or not be:
                    return str(block.get("note") or "Зал занят по регулярному расписанию.")
                if self._overlap(seg_start, seg_end, str(bs), str(be)):
                    return str(block.get("note") or "Зал занят по регулярному расписанию.")
        return None

    async def cleanup_holds(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        async with self.db.connection() as conn:
            await conn.execute(
                "UPDATE slot_holds SET status='expired',updated_at=? WHERE status='active' AND expires_at<=?",
                (utc_now_iso(), now),
            )
            changed = await (await conn.execute("SELECT changes() c")).fetchone()
            await conn.commit()
        return int(changed["c"] if changed else 0)

    @staticmethod
    def _minutes(value: str) -> int:
        h, m = value.split(":", 1); return int(h) * 60 + int(m)

    @classmethod
    def _overlap(cls, a1: str, a2: str, b1: str, b2: str) -> bool:
        aa1 = cls._minutes(a1); aa2 = 1440 if a2 == "24:00" else cls._minutes(a2)
        bb1 = cls._minutes(b1); bb2 = 1440 if b2 == "24:00" else cls._minutes(b2)
        return aa1 < bb2 and aa2 > bb1

    async def hold_conflicts(self, *, venue_id: int | None, date_iso: str, start_time: str, end_time: str, exclude_token: str = "") -> bool:
        if not venue_id:
            return False
        await self.cleanup_holds()
        async with self.db.connection() as conn:
            rows = await (await conn.execute(
                "SELECT * FROM slot_holds WHERE venue_id=? AND date_iso=? AND status='active' AND token<>?",
                (venue_id, date_iso, exclude_token),
            )).fetchall()
        return any(self._overlap(start_time, end_time, str(r["start_time"]), str(r["end_time"])) for r in rows)

    async def acquire_hold(self, *, form_id: int, venue_id: int, chat_id: int, submission_token: str, segments: list[tuple[str,str,str]]) -> tuple[bool, str]:
        if not segments:
            return True, ""
        window = max(1, int(await self.db.get_setting("hold_rate_window_minutes", "10") or 10))
        limit = max(1, int(await self.db.get_setting("hold_rate_max", "5") or 5))
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window)).isoformat()
        async with self.db.connection() as conn:
            recent = await (await conn.execute(
                "SELECT COUNT(*) c FROM slot_holds WHERE chat_id=? AND created_at>=?",
                (chat_id, cutoff),
            )).fetchone()
        if int(recent["c"] or 0) >= limit:
            return False, f"Слишком много временных резервов. Повторите через {window} мин."
        minutes = max(1, min(int(await self.db.get_setting("slot_hold_minutes", "15") or 15), 120))
        await self.cleanup_holds()
        for date_iso, start_time, end_time in segments:
            if await self.availability_conflicts(venue_id=venue_id, date_iso=date_iso, start_time=start_time, end_time=end_time, exclude_hold_token=submission_token):
                return False, "Этот слот уже занят или только что выбран другим клиентом. Попробуйте другое время."
        now = datetime.now(timezone.utc); expires = now + timedelta(minutes=minutes)
        async with self.db.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute("UPDATE slot_holds SET status='released',updated_at=? WHERE submission_token=? AND status='active'", (utc_now_iso(), submission_token))
            for idx, (date_iso, start_time, end_time) in enumerate(segments):
                await conn.execute(
                    "INSERT INTO slot_holds(token,submission_token,form_id,venue_id,chat_id,date_iso,start_time,end_time,status,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?, 'active',?,?,?)",
                    (f"{submission_token}:{idx}:{secrets.token_hex(4)}", submission_token, form_id, venue_id, chat_id, date_iso, start_time, end_time, expires.isoformat(), utc_now_iso(), utc_now_iso()),
                )
            await conn.commit()
        return True, f"Слот удерживается {minutes} мин."

    async def release_holds(self, submission_token: str, *, consumed: bool = False) -> None:
        async with self.db.connection() as conn:
            await conn.execute(
                "UPDATE slot_holds SET status=?,updated_at=? WHERE submission_token=? AND status='active'",
                ("consumed" if consumed else "released", utc_now_iso(), submission_token),
            )
            await conn.commit()

    async def list_active_holds(self, limit: int = 100) -> list[dict[str, Any]]:
        await self.cleanup_holds()
        async with self.db.connection() as conn:
            rows = await (await conn.execute(
                """SELECT h.*,v.name venue_name FROM slot_holds h LEFT JOIN venues v ON v.id=h.venue_id
                   WHERE h.status='active' ORDER BY h.expires_at LIMIT ?""", (max(1,min(limit,500)),)
            )).fetchall()
        return [dict(r) for r in rows]

    async def add_waitlist(self, **values: Any) -> int:
        now = utc_now_iso()
        async with self.db.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO waitlist_entries(form_id,venue_id,chat_id,business_connection_id,user_id,username,date_iso,start_time,end_time,note,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,'waiting',?,?)""",
                (values.get("form_id"), values.get("venue_id"), values["chat_id"], values.get("business_connection_id"), values.get("user_id"), values.get("username"), values["date_iso"], values.get("start_time"), values.get("end_time"), str(values.get("note") or "")[:1000], now, now),
            )
            await conn.commit(); return int(cur.lastrowid)

    async def list_waitlist(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT w.*,v.name venue_name,f.name form_name FROM waitlist_entries w LEFT JOIN venues v ON v.id=w.venue_id LEFT JOIN forms f ON f.id=w.form_id"
        args: list[Any] = []
        if status:
            sql += " WHERE w.status=?"; args.append(status)
        sql += " ORDER BY w.created_at DESC LIMIT ?"; args.append(max(1,min(limit,500)))
        async with self.db.connection() as conn:
            rows = await (await conn.execute(sql, tuple(args))).fetchall()
        return [dict(r) for r in rows]

    async def set_waitlist_status(self, entry_id: int, status: str) -> None:
        if status not in {"waiting","notified","booked","cancelled"}: raise ValueError("status")
        async with self.db.connection() as conn:
            await conn.execute("UPDATE waitlist_entries SET status=?,updated_at=? WHERE id=?", (status,utc_now_iso(),entry_id)); await conn.commit()

    async def list_resources(self) -> list[dict[str, Any]]:
        async with self.db.connection() as conn:
            rows = await (await conn.execute("SELECT r.*,v.name venue_name FROM resources r LEFT JOIN venues v ON v.id=r.venue_id ORDER BY r.position,r.id")).fetchall()
        return [dict(r) for r in rows]

    async def save_resource(self, *, resource_id: int | None, name: str, quantity: int, venue_id: int | None, enabled: bool = True) -> int:
        now=utc_now_iso(); quantity=max(0,min(int(quantity),100000))
        async with self.db.connection() as conn:
            if resource_id:
                await conn.execute("UPDATE resources SET name=?,quantity=?,venue_id=?,enabled=?,updated_at=? WHERE id=?", (name[:150],quantity,venue_id,1 if enabled else 0,now,resource_id)); rid=resource_id
            else:
                cur=await conn.execute("INSERT INTO resources(venue_id,name,quantity,enabled,position,created_at,updated_at) VALUES(?,?,?, ?,100,?,?)", (venue_id,name[:150],quantity,1 if enabled else 0,now,now)); rid=int(cur.lastrowid)
            await conn.commit(); return int(rid)

    async def delete_resource(self, resource_id: int) -> None:
        async with self.db.connection() as conn:
            await conn.execute("DELETE FROM resources WHERE id=?",(resource_id,)); await conn.commit()

    async def set_resource_allocation(self, submission_id: int, resource_id: int, quantity: int) -> None:
        quantity=max(0,int(quantity))
        async with self.db.connection() as conn:
            resource=await (await conn.execute("SELECT * FROM resources WHERE id=?",(resource_id,))).fetchone()
            if not resource:
                raise ValueError("Ресурс не найден")
            if quantity > int(resource["quantity"] or 0):
                raise ValueError("Запрошено больше единиц, чем есть в наличии")
            if quantity > 0:
                target_blocks=await (await conn.execute("SELECT date_iso,start_time,end_time FROM availability_blocks WHERE source_submission_id=?",(submission_id,))).fetchall()
                used=0
                rows=await (await conn.execute("""SELECT ra.submission_id,ra.quantity,ab.date_iso,ab.start_time,ab.end_time
                    FROM resource_allocations ra JOIN availability_blocks ab ON ab.source_submission_id=ra.submission_id
                    JOIN form_submissions fs ON fs.id=ra.submission_id
                    WHERE ra.resource_id=? AND ra.submission_id<>? AND fs.status IN ('confirmed','paid','completed')""",(resource_id,submission_id))).fetchall()
                overlapping_submissions:set[int]=set()
                for t in target_blocks:
                    for r in rows:
                        if str(t["date_iso"])!=str(r["date_iso"]):
                            continue
                        if not t["start_time"] or not t["end_time"] or not r["start_time"] or not r["end_time"] or self._overlap(str(t["start_time"]),str(t["end_time"]),str(r["start_time"]),str(r["end_time"])):
                            overlapping_submissions.add(int(r["submission_id"]))
                if overlapping_submissions:
                    ph=','.join('?' for _ in overlapping_submissions)
                    q=await (await conn.execute(f"SELECT COALESCE(SUM(quantity),0) q FROM resource_allocations WHERE resource_id=? AND submission_id IN ({ph})",(resource_id,*overlapping_submissions))).fetchone()
                    used=int(q["q"] or 0) if q else 0
                if used + quantity > int(resource["quantity"] or 0):
                    raise ValueError(f"Недостаточно ресурса: свободно {max(0,int(resource['quantity'] or 0)-used)}")
            if quantity <= 0:
                await conn.execute("DELETE FROM resource_allocations WHERE submission_id=? AND resource_id=?", (submission_id,resource_id))
            else:
                await conn.execute("INSERT INTO resource_allocations(submission_id,resource_id,quantity,created_at) VALUES(?,?,?,?) ON CONFLICT(submission_id,resource_id) DO UPDATE SET quantity=excluded.quantity", (submission_id,resource_id,quantity,utc_now_iso()))
            await conn.commit()

    async def list_resource_allocations(self, submission_id: int) -> list[dict[str, Any]]:
        async with self.db.connection() as conn:
            rows = await (await conn.execute(
                """SELECT ra.*,r.name resource_name,r.quantity resource_total,v.name venue_name
                   FROM resource_allocations ra JOIN resources r ON r.id=ra.resource_id
                   LEFT JOIN venues v ON v.id=r.venue_id WHERE ra.submission_id=?
                   ORDER BY r.position,r.id""", (submission_id,)
            )).fetchall()
        return [dict(r) for r in rows]

    async def list_packages(self) -> list[dict[str, Any]]:
        async with self.db.connection() as conn:
            rows=await (await conn.execute("SELECT p.*,v.name venue_name,f.name form_name FROM service_packages p LEFT JOIN venues v ON v.id=p.venue_id LEFT JOIN forms f ON f.id=p.form_id ORDER BY p.position,p.id")).fetchall()
        return [dict(r) for r in rows]

    async def save_package(self, *, package_id: int | None, name: str, description: str, amount: int, form_id: int | None, venue_id: int | None, enabled: bool=True) -> int:
        now=utc_now_iso(); amount=max(0,int(amount))
        async with self.db.connection() as conn:
            if package_id:
                await conn.execute("UPDATE service_packages SET form_id=?,venue_id=?,name=?,description=?,amount=?,enabled=?,updated_at=? WHERE id=?", (form_id,venue_id,name[:150],description[:3000],amount,1 if enabled else 0,now,package_id)); pid=package_id
            else:
                cur=await conn.execute("INSERT INTO service_packages(form_id,venue_id,name,description,amount,enabled,position,created_at,updated_at) VALUES(?,?,?,?,?,?,100,?,?)", (form_id,venue_id,name[:150],description[:3000],amount,1 if enabled else 0,now,now)); pid=int(cur.lastrowid)
            await conn.commit(); return int(pid)

    async def apply_package(self, submission_id: int, package_id: int) -> int:
        now = utc_now_iso()
        async with self.db.connection() as conn:
            package = await (await conn.execute("SELECT * FROM service_packages WHERE id=? AND enabled=1", (package_id,))).fetchone()
            submission = await (await conn.execute("SELECT form_id,venue_id,total_amount FROM form_submissions WHERE id=?", (submission_id,))).fetchone()
            if not package or not submission:
                raise ValueError("Пакет или заявка не найдены")
            if package["form_id"] is not None and int(package["form_id"]) != int(submission["form_id"] or 0):
                raise ValueError("Пакет предназначен для другой формы")
            if package["venue_id"] is not None and int(package["venue_id"]) != int(submission["venue_id"] or 0):
                raise ValueError("Пакет предназначен для другого зала")
            old_total = int(submission["total_amount"] or 0)
            new_total = old_total + max(0, int(package["amount"] or 0))
            await conn.execute("UPDATE form_submissions SET total_amount=?,updated_at=? WHERE id=?", (new_total, now, submission_id))
            await conn.execute(
                "INSERT INTO submission_events(submission_id,event_type,old_value,new_value,created_at) VALUES(?,'package_applied',?,?,?)",
                (submission_id, str(old_total), f"{new_total}:{package['name']}", now),
            )
            await conn.commit()
            return new_total

    async def ensure_submission_public_token(self, submission_id: int) -> str:
        async with self.db.connection() as conn:
            row=await (await conn.execute("SELECT public_token FROM form_submissions WHERE id=?",(submission_id,))).fetchone()
            token=str(row["public_token"] or "") if row else ""
            if not token:
                token=secrets.token_hex(16)
                await conn.execute("UPDATE form_submissions SET public_token=?,updated_at=? WHERE id=?",(token,utc_now_iso(),submission_id))
                await conn.commit()
            return token

    async def assign_manager(self, submission_id: int, manager: str) -> None:
        async with self.db.connection() as conn:
            await conn.execute("UPDATE form_submissions SET manager=?,updated_at=? WHERE id=?", (manager[:100],utc_now_iso(),submission_id))
            await conn.execute("INSERT INTO submission_events(submission_id,event_type,new_value,created_at) VALUES(?,'manager',?,?)", (submission_id,manager[:100],utc_now_iso())); await conn.commit()

    async def add_task(self, submission_id: int, title: str, *, assignee: str="", due_at: str|None=None, created_by: str="") -> int:
        now=utc_now_iso()
        async with self.db.connection() as conn:
            cur=await conn.execute("INSERT INTO submission_tasks(submission_id,title,status,assignee,due_at,created_by,created_at,updated_at) VALUES(?,?,'open',?,?,?,?,?)", (submission_id,title[:500],assignee[:100],due_at,created_by[:100],now,now))
            await conn.execute("INSERT INTO submission_events(submission_id,event_type,new_value,created_at) VALUES(?,'task_created',?,?)",(submission_id,title[:500],now))
            await conn.commit(); return int(cur.lastrowid)

    async def list_tasks(self, submission_id: int | None=None, status: str|None=None, limit: int=200) -> list[dict[str,Any]]:
        clauses=[]; args=[]
        if submission_id is not None: clauses.append("t.submission_id=?"); args.append(submission_id)
        if status: clauses.append("t.status=?"); args.append(status)
        sql="SELECT t.*,s.form_name FROM submission_tasks t JOIN form_submissions s ON s.id=t.submission_id"
        if clauses: sql += " WHERE "+" AND ".join(clauses)
        sql += " ORDER BY CASE WHEN t.due_at IS NULL THEN 1 ELSE 0 END,t.due_at,t.id DESC LIMIT ?"; args.append(max(1,min(limit,500)))
        async with self.db.connection() as conn:
            rows=await (await conn.execute(sql,tuple(args))).fetchall()
        return [dict(r) for r in rows]

    async def set_task_status(self, task_id: int, status: str) -> None:
        if status not in {"open","done","cancelled"}: raise ValueError("status")
        async with self.db.connection() as conn:
            await conn.execute("UPDATE submission_tasks SET status=?,updated_at=? WHERE id=?",(status,utc_now_iso(),task_id)); await conn.commit()

    async def update_deposit(self, submission_id: int, amount: int, status: str) -> None:
        if status not in {"none","required","paid","returned","withheld"}: raise ValueError("status")
        amount=max(0,int(amount)); now=utc_now_iso()
        async with self.db.connection() as conn:
            old=await (await conn.execute("SELECT deposit_amount,deposit_status FROM form_submissions WHERE id=?",(submission_id,))).fetchone()
            await conn.execute("UPDATE form_submissions SET deposit_amount=?,deposit_status=?,updated_at=? WHERE id=?",(amount,status,now,submission_id))
            await conn.execute("INSERT INTO submission_events(submission_id,event_type,old_value,new_value,created_at) VALUES(?,'deposit',?,?,?)",(submission_id,f"{int(old['deposit_amount'] or 0) if old else 0}:{str(old['deposit_status'] or 'none') if old else 'none'}",f"{amount}:{status}",now))
            await conn.commit()

    async def archive_old(self) -> int:
        days=max(30,min(int(await self.db.get_setting("archive_after_days","365") or 365),3650))
        cutoff=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
        async with self.db.connection() as conn:
            await conn.execute("UPDATE form_submissions SET archived=1 WHERE archived=0 AND status IN ('completed','cancelled') AND created_at<?",(cutoff,))
            row=await (await conn.execute("SELECT changes() c")).fetchone(); await conn.commit()
        return int(row["c"] if row else 0)

    async def create_public_request(self, *, venue_id: int|None, name: str, contact: str, date_iso: str, start_time: str|None, end_time: str|None, guests: str, comment: str) -> int:
        now=utc_now_iso()
        async with self.db.connection() as conn:
            cur=await conn.execute("INSERT INTO public_booking_requests(venue_id,name,contact,date_iso,start_time,end_time,guests,comment,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'new',?,?)",(venue_id,name[:200],contact[:200],date_iso,start_time,end_time,guests[:100],comment[:3000],now,now)); await conn.commit(); return int(cur.lastrowid)

    async def list_public_requests(self, limit: int=200) -> list[dict[str,Any]]:
        async with self.db.connection() as conn:
            rows=await (await conn.execute("SELECT p.*,v.name venue_name FROM public_booking_requests p LEFT JOIN venues v ON v.id=p.venue_id ORDER BY p.created_at DESC LIMIT ?",(max(1,min(limit,500)),))).fetchall()
        return [dict(r) for r in rows]

    async def emit_webhook(self, event_type: str, payload: dict[str,Any]) -> None:
        url=str(await self.db.get_setting("webhook_url","") or "").strip()
        secret=str(await self.db.get_setting("webhook_secret","") or "")
        now=utc_now_iso(); raw=json.dumps(payload,ensure_ascii=False,separators=(",",":"))
        async with self.db.connection() as conn:
            cur=await conn.execute("INSERT INTO webhook_events(event_type,payload_json,status,created_at,updated_at) VALUES(?,?,'pending',?,?)",(event_type,raw,now,now)); event_id=int(cur.lastrowid); await conn.commit()
        if not url:
            return
        sig=hmac.new(secret.encode(),raw.encode(),hashlib.sha256).hexdigest() if secret else ""
        try:
            async with ClientSession(timeout=ClientTimeout(total=8)) as session:
                async with session.post(url,json={"event":event_type,"data":payload},headers={"X-TGAutoreply-Signature":sig}) as resp:
                    if resp.status >= 300:
                        raise RuntimeError(f"HTTP {resp.status}")
            status="sent"; err=""
        except Exception as exc:
            status="error"; err=str(exc)[:500]
        async with self.db.connection() as conn:
            await conn.execute("UPDATE webhook_events SET status=?,attempts=attempts+1,last_error=?,updated_at=? WHERE id=?",(status,err,utc_now_iso(),event_id)); await conn.commit()

    async def month_availability(self, year: int, month: int, venue_id: int | None) -> tuple[set[str], set[str]]:
        prefix=f"{year:04d}-{month:02d}-"
        clauses=["date_iso LIKE ?"]; args:[Any]=[prefix+"%"]
        if venue_id is not None:
            clauses.append("(venue_id IS NULL OR venue_id=?)"); args.append(int(venue_id))
        async with self.db.connection() as conn:
            rows=await (await conn.execute("SELECT date_iso,start_time,end_time FROM availability_blocks WHERE "+" AND ".join(clauses),tuple(args))).fetchall()
        full:set[str]=set(); partial:set[str]=set()
        for row in rows:
            d=str(row["date_iso"]); st=row["start_time"]; en=row["end_time"]
            if not st or not en:
                full.add(d); partial.discard(d)
            elif d not in full:
                partial.add(d)
        return full,partial

    async def date_fully_busy(self, date_iso: str, venue_id: int | None) -> bool:
        clauses=["date_iso=?"]; args:[Any]=[date_iso]
        if venue_id is not None:
            clauses.append("(venue_id IS NULL OR venue_id=?)"); args.append(int(venue_id))
        async with self.db.connection() as conn:
            rows=await (await conn.execute("SELECT start_time,end_time FROM availability_blocks WHERE "+" AND ".join(clauses),tuple(args))).fetchall()
        return any(not r["start_time"] or not r["end_time"] for r in rows)

    async def availability_conflicts(self, *, venue_id: int | None, date_iso: str, start_time: str | None, end_time: str | None, exclude_submission_id: int | None = None, exclude_hold_token: str = "") -> bool:
        clauses=["date_iso=?"]; args:[Any]=[date_iso]
        if venue_id is not None:
            clauses.append("(venue_id IS NULL OR venue_id=?)"); args.append(int(venue_id))
        if exclude_submission_id is not None:
            clauses.append("(source_submission_id IS NULL OR source_submission_id<>?)"); args.append(int(exclude_submission_id))
        async with self.db.connection() as conn:
            rows=await (await conn.execute("SELECT * FROM availability_blocks WHERE "+" AND ".join(clauses),tuple(args))).fetchall()
        for row in rows:
            bs=row["start_time"]; be=row["end_time"]
            if not bs or not be or not start_time or not end_time:
                return True
            if self._overlap(str(start_time),str(end_time),str(bs),str(be)):
                return True
        return await self.hold_conflicts(venue_id=venue_id,date_iso=date_iso,start_time=str(start_time or "00:00"),end_time=str(end_time or "24:00"),exclude_token=exclude_hold_token)

    async def health_snapshot(self) -> dict[str,Any]:
        async with self.db.connection() as conn:
            db_size=await (await conn.execute("PRAGMA page_count")).fetchone(); page_size=await (await conn.execute("PRAGMA page_size")).fetchone()
            counts={}
            for key,sql in {
                "submissions":"SELECT COUNT(*) c FROM form_submissions WHERE archived=0",
                "holds":"SELECT COUNT(*) c FROM slot_holds WHERE status='active'",
                "waitlist":"SELECT COUNT(*) c FROM waitlist_entries WHERE status='waiting'",
                "tasks":"SELECT COUNT(*) c FROM submission_tasks WHERE status='open'",
                "public_requests":"SELECT COUNT(*) c FROM public_booking_requests WHERE status='new'",
            }.items():
                row=await (await conn.execute(sql)).fetchone(); counts[key]=int(row["c"] if row else 0)
        counts["db_bytes"]=int((db_size[0] if db_size else 0)*(page_size[0] if page_size else 0))
        counts["venues"]=len(await self.list_venues())
        return counts
