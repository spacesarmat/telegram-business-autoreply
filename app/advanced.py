from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .db import Database, utc_now_iso


WEEKDAY_NAMES = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return fallback
    return parsed


def _minutes(value: str | None) -> int | None:
    if not value:
        return None
    try:
        h, m = str(value).split(":", 1)
        h_i, m_i = int(h), int(m)
    except (TypeError, ValueError):
        return None
    if not (0 <= h_i <= 23 and 0 <= m_i <= 59):
        return None
    return h_i * 60 + m_i


def _overlap(start1: str, end1: str, start2: str, end2: str) -> bool:
    a1 = _minutes(start1)
    a2 = 24 * 60 if end1 == "24:00" else _minutes(end1)
    b1 = _minutes(start2)
    b2 = 24 * 60 if end2 == "24:00" else _minutes(end2)
    if None in {a1, a2, b1, b2}:
        return False
    return int(a1) < int(b2) and int(a2) > int(b1)


class AdvancedService:
    """v2.0 business rules, CRM analytics and integrations.

    Kept separate from Database so the older DB API stays compatible and future
    features can be evolved without disturbing Telegram form logic.
    """

    def __init__(self, db: Database, timezone: ZoneInfo) -> None:
        self.db = db
        self.timezone = timezone

    async def init(self) -> None:
        async with self.db.connection() as conn:
            await conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS booking_rules (
                    form_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    min_duration_minutes INTEGER NOT NULL DEFAULT 0,
                    min_lead_hours INTEGER NOT NULL DEFAULT 0,
                    max_advance_days INTEGER NOT NULL DEFAULT 365,
                    closed_weekdays_json TEXT NOT NULL DEFAULT '[]',
                    day_hours_json TEXT NOT NULL DEFAULT '{}',
                    guest_surcharges_json TEXT NOT NULL DEFAULT '{}',
                    weekday_surcharges_json TEXT NOT NULL DEFAULT '{}',
                    night_start TEXT NOT NULL DEFAULT '23:00',
                    night_surcharge_amount INTEGER NOT NULL DEFAULT 0,
                    night_surcharge_percent INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS recurring_blocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    form_id INTEGER,
                    weekday INTEGER NOT NULL,
                    start_time TEXT,
                    end_time TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_recurring_blocks_weekday
                    ON recurring_blocks(weekday, enabled, form_id);

                CREATE TABLE IF NOT EXISTS client_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_key TEXT NOT NULL,
                    note TEXT NOT NULL,
                    admin_name TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_client_notes_key
                    ON client_notes(client_key, created_at DESC);

                CREATE TABLE IF NOT EXISTS web_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT,
                    details TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_web_audit_created ON web_audit(created_at DESC);

                CREATE TABLE IF NOT EXISTS client_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    request_type TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(submission_id) REFERENCES form_submissions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_client_requests_status
                    ON client_requests(status, created_at DESC);
                """
            )
            await conn.commit()

        defaults = {
            "calendar_feed_enabled": "0",
            "calendar_feed_token": secrets.token_urlsafe(24),
            "payment_link_template": "",
            "payment_message_template": (
                "💳 Предоплата по заявке №{id}\n\n"
                "К оплате: {prepayment}\nСумма заявки: {amount}\nОстаток: {balance}\n\n"
                "Ссылка: {url}"
            ),
            "payment_default_percent": "30",
        }
        for key, value in defaults.items():
            current = await self.db.get_setting(key, None)
            if current is None:
                await self.db.set_setting(key, value)

    async def get_rule(self, form_id: int) -> dict[str, Any]:
        async with self.db.connection() as conn:
            row = await (await conn.execute("SELECT * FROM booking_rules WHERE form_id=?", (form_id,))).fetchone()
        if not row:
            return {
                "form_id": form_id, "enabled": 0, "min_duration_minutes": 0,
                "min_lead_hours": 0, "max_advance_days": 365,
                "closed_weekdays": [], "day_hours": {}, "guest_surcharges": {},
                "weekday_surcharges": {}, "night_start": "23:00",
                "night_surcharge_amount": 0, "night_surcharge_percent": 0,
            }
        item = dict(row)
        item["closed_weekdays"] = [int(x) for x in _json(item.get("closed_weekdays_json"), []) if str(x).isdigit()]
        item["day_hours"] = _json(item.get("day_hours_json"), {})
        item["guest_surcharges"] = _json(item.get("guest_surcharges_json"), {})
        item["weekday_surcharges"] = _json(item.get("weekday_surcharges_json"), {})
        return item

    async def update_rule(self, form_id: int, values: dict[str, Any]) -> None:
        allowed = {
            "enabled", "min_duration_minutes", "min_lead_hours", "max_advance_days",
            "closed_weekdays_json", "day_hours_json", "guest_surcharges_json",
            "weekday_surcharges_json", "night_start", "night_surcharge_amount",
            "night_surcharge_percent",
        }
        clean = {k: v for k, v in values.items() if k in allowed}
        now = utc_now_iso()
        async with self.db.connection() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO booking_rules(form_id, updated_at) VALUES(?, ?)",
                (form_id, now),
            )
            for field, value in clean.items():
                await conn.execute(
                    f"UPDATE booking_rules SET {field}=?, updated_at=? WHERE form_id=?",
                    (value, now, form_id),
                )
            await conn.commit()

    async def list_recurring_blocks(self, form_id: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM recurring_blocks"
        args: tuple[Any, ...] = ()
        if form_id is not None:
            sql += " WHERE form_id IS NULL OR form_id=?"
            args = (form_id,)
        sql += " ORDER BY weekday, COALESCE(start_time,''), id"
        async with self.db.connection() as conn:
            rows = await (await conn.execute(sql, args)).fetchall()
        return [dict(row) for row in rows]

    async def add_recurring_block(
        self, *, form_id: int | None, weekday: int, start_time: str | None,
        end_time: str | None, note: str, enabled: bool = True,
    ) -> int:
        if not 0 <= weekday <= 6:
            raise ValueError("weekday")
        now = utc_now_iso()
        async with self.db.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO recurring_blocks(form_id, weekday, start_time, end_time, note, enabled, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (form_id, weekday, start_time, end_time, note[:200], 1 if enabled else 0, now, now),
            )
            await conn.commit()
            return int(cur.lastrowid)

    async def delete_recurring_block(self, block_id: int) -> None:
        async with self.db.connection() as conn:
            await conn.execute("DELETE FROM recurring_blocks WHERE id=?", (block_id,))
            await conn.commit()

    async def booking_rule_violation(self, form_id: int, interval: dict | None) -> str | None:
        if not interval:
            return None
        rule = await self.get_rule(form_id)
        date_iso = str(interval.get("date_iso") or "")
        start_time = str(interval.get("start_time") or "")
        duration = int(interval.get("duration_minutes") or 0)
        try:
            start_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
        except ValueError:
            return None

        if rule.get("enabled"):
            min_duration = max(0, int(rule.get("min_duration_minutes") or 0))
            if min_duration and duration < min_duration:
                h, m = divmod(min_duration, 60)
                label = f"{h} ч" + (f" {m} мин" if m else "")
                return f"Минимальная длительность аренды — {label}."

            start_min = _minutes(start_time)
            if start_min is not None:
                start_local = datetime.combine(start_date, datetime.min.time(), self.timezone) + timedelta(minutes=start_min)
                lead = max(0, int(rule.get("min_lead_hours") or 0))
                if lead and start_local < datetime.now(self.timezone) + timedelta(hours=lead):
                    return f"Бронирование возможно минимум за {lead} ч до начала."
                horizon = max(0, int(rule.get("max_advance_days") or 0))
                if horizon and start_date > datetime.now(self.timezone).date() + timedelta(days=horizon):
                    return f"Бронирование доступно максимум на {horizon} дней вперёд."

            if start_date.weekday() in set(rule.get("closed_weekdays") or []):
                return f"По {WEEKDAY_NAMES[start_date.weekday()]} площадка закрыта для бронирования."

            day_hours = rule.get("day_hours") or {}
            for seg_date, seg_start, seg_end in interval.get("segments") or []:
                try:
                    day = datetime.strptime(seg_date, "%Y-%m-%d").date()
                except ValueError:
                    continue
                window = day_hours.get(str(day.weekday())) or day_hours.get(day.weekday())
                if not window or not isinstance(window, (list, tuple)) or len(window) != 2:
                    continue
                ws, we = str(window[0]), str(window[1])
                # Whole segment must be within its daily operating window.
                s, e = _minutes(seg_start), 24 * 60 if seg_end == "24:00" else _minutes(seg_end)
                a, b = _minutes(ws), 24 * 60 if we == "24:00" else _minutes(we)
                if None not in {s, e, a, b} and not (int(s) >= int(a) and int(e) <= int(b)):
                    return f"На {WEEKDAY_NAMES[day.weekday()]} доступное время: {ws}–{we}."

        blocks = await self.list_recurring_blocks(form_id)
        for seg_date, seg_start, seg_end in interval.get("segments") or []:
            try:
                weekday = datetime.strptime(seg_date, "%Y-%m-%d").date().weekday()
            except ValueError:
                continue
            for block in blocks:
                if not block.get("enabled") or int(block.get("weekday") or -1) != weekday:
                    continue
                bs, be = block.get("start_time"), block.get("end_time")
                if not bs or not be:
                    return str(block.get("note") or "Это время занято по регулярному расписанию.")
                if _overlap(seg_start, seg_end, str(bs), str(be)):
                    return str(block.get("note") or "Это время занято по регулярному расписанию.")
        return None

    async def recurring_violation(self, form_id: int, segments: list[tuple[str, str, str]]) -> str | None:
        blocks = await self.list_recurring_blocks(form_id)
        for seg_date, seg_start, seg_end in segments:
            try:
                weekday = datetime.strptime(seg_date, "%Y-%m-%d").date().weekday()
            except ValueError:
                continue
            for block in blocks:
                if not block.get("enabled") or int(block.get("weekday") or -1) != weekday:
                    continue
                bs, be = block.get("start_time"), block.get("end_time")
                if not bs or not be:
                    return str(block.get("note") or "Это время занято по регулярному расписанию.")
                if _overlap(seg_start, seg_end, str(bs), str(be)):
                    return str(block.get("note") or "Это время занято по регулярному расписанию.")
        return None

    async def pricing_surcharges(
        self, form_id: int, questions: list[dict], answers: dict[str, str], interval: dict | None,
        rental_amount: int,
    ) -> dict[str, Any]:
        rule = await self.get_rule(form_id)
        if not rule.get("enabled"):
            return {"total": 0, "items": []}
        items: list[dict[str, Any]] = []

        guest_answer = None
        for q in questions:
            if str(q.get("input_type") or "") == "guest_count":
                guest_answer = answers.get(str(q.get("id")))
                break
        guest_map = rule.get("guest_surcharges") or {}
        if guest_answer and str(guest_answer) in guest_map:
            try:
                amount = max(0, int(guest_map[str(guest_answer)] or 0))
            except (TypeError, ValueError):
                amount = 0
            if amount:
                items.append({"kind": "guests", "label": f"Гости: {guest_answer}", "amount": amount})

        if interval and interval.get("date_iso"):
            try:
                weekday = datetime.strptime(str(interval["date_iso"]), "%Y-%m-%d").date().weekday()
            except ValueError:
                weekday = None
            if weekday is not None:
                percent_raw = (rule.get("weekday_surcharges") or {}).get(str(weekday), 0)
                try:
                    percent = max(0, int(percent_raw or 0))
                except (TypeError, ValueError):
                    percent = 0
                if percent:
                    amount = (max(0, int(rental_amount)) * percent + 99) // 100
                    items.append({"kind": "weekday", "label": f"{WEEKDAY_NAMES[weekday]} +{percent}%", "amount": amount})

            night_start = _minutes(str(rule.get("night_start") or "23:00"))
            start = _minutes(str(interval.get("start_time") or ""))
            is_night = bool(interval.get("overnight")) or (
                night_start is not None and start is not None and start >= night_start
            )
            if is_night:
                fixed = max(0, int(rule.get("night_surcharge_amount") or 0))
                percent = max(0, int(rule.get("night_surcharge_percent") or 0))
                if fixed:
                    items.append({"kind": "night", "label": "Ночная аренда", "amount": fixed})
                if percent:
                    amount = (max(0, int(rental_amount)) * percent + 99) // 100
                    items.append({"kind": "night_percent", "label": f"Ночная аренда +{percent}%", "amount": amount})

        return {"total": sum(int(x["amount"]) for x in items), "items": items}

    @staticmethod
    def client_key(item: dict[str, Any]) -> str:
        if item.get("user_id"):
            return f"u:{int(item['user_id'])}"
        return f"c:{int(item.get('chat_id') or 0)}"

    async def list_clients(self, query: str = "", limit: int = 200) -> list[dict[str, Any]]:
        query = query.strip().lstrip("@")
        like = f"%{query}%"
        where = ""
        args: list[Any] = []
        if query:
            where = "WHERE COALESCE(username,'') LIKE ? COLLATE NOCASE OR COALESCE(first_name,'') LIKE ? COLLATE NOCASE OR COALESCE(last_name,'') LIKE ? COLLATE NOCASE OR answers_json LIKE ? COLLATE NOCASE"
            args.extend([like, like, like, like])
        sql = f"""
            SELECT COALESCE(CAST(user_id AS TEXT), 'chat:' || CAST(chat_id AS TEXT)) AS client_group,
                   MAX(user_id) AS user_id, MAX(chat_id) AS chat_id,
                   MAX(username) AS username, MAX(first_name) AS first_name, MAX(last_name) AS last_name,
                   COUNT(*) AS submissions_count,
                   SUM(CASE WHEN status != 'cancelled' THEN total_amount ELSE 0 END) AS total_amount,
                   SUM(CASE WHEN status IN ('confirmed','paid','completed') THEN 1 ELSE 0 END) AS successful_count,
                   MAX(created_at) AS last_submission_at
            FROM form_submissions {where}
            GROUP BY client_group
            ORDER BY last_submission_at DESC
            LIMIT ?
        """
        args.append(max(1, min(int(limit), 500)))
        async with self.db.connection() as conn:
            rows = await (await conn.execute(sql, tuple(args))).fetchall()
        return [dict(r) for r in rows]

    async def get_client(self, client_key: str) -> dict[str, Any] | None:
        if client_key.startswith("u:") and client_key[2:].isdigit():
            uid = int(client_key[2:])
            where, arg = "user_id=?", uid
        elif client_key.startswith("c:") and client_key[2:].lstrip("-").isdigit():
            cid = int(client_key[2:])
            where, arg = "chat_id=?", cid
        else:
            return None
        async with self.db.connection() as conn:
            row = await (await conn.execute(
                f"""SELECT MAX(user_id) user_id, MAX(chat_id) chat_id, MAX(username) username,
                            MAX(first_name) first_name, MAX(last_name) last_name,
                            COUNT(*) submissions_count,
                            SUM(CASE WHEN status != 'cancelled' THEN total_amount ELSE 0 END) total_amount,
                            MAX(created_at) last_submission_at
                     FROM form_submissions WHERE {where}""", (arg,)
            )).fetchone()
        return dict(row) if row and row["submissions_count"] else None

    async def add_client_note(self, client_key: str, note: str, admin_name: str) -> None:
        async with self.db.connection() as conn:
            await conn.execute(
                "INSERT INTO client_notes(client_key,note,admin_name,created_at) VALUES(?,?,?,?)",
                (client_key, note[:4000], admin_name[:100], utc_now_iso()),
            )
            await conn.commit()

    async def list_client_notes(self, client_key: str, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.connection() as conn:
            rows = await (await conn.execute(
                "SELECT * FROM client_notes WHERE client_key=? ORDER BY created_at DESC,id DESC LIMIT ?",
                (client_key, max(1, min(int(limit), 100))),
            )).fetchall()
        return [dict(r) for r in rows]

    async def add_audit(self, actor: str, role: str, action: str, target: str = "", details: str = "") -> None:
        async with self.db.connection() as conn:
            await conn.execute(
                "INSERT INTO web_audit(actor,role,action,target,details,created_at) VALUES(?,?,?,?,?,?)",
                (actor[:100], role[:30], action[:200], target[:300], details[:2000], utc_now_iso()),
            )
            await conn.commit()

    async def list_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        async with self.db.connection() as conn:
            rows = await (await conn.execute(
                """
                SELECT actor, role, action, target, details, created_at FROM (
                    SELECT actor, role, action, target, details, created_at, id AS sort_id
                    FROM web_audit
                    UNION ALL
                    SELECT CASE WHEN admin_user_id IS NULL THEN 'system' ELSE 'tg:' || CAST(admin_user_id AS TEXT) END AS actor,
                           'telegram' AS role,
                           event_type AS action,
                           'submission #' || CAST(submission_id AS TEXT) AS target,
                           COALESCE(old_value,'') || CASE WHEN old_value IS NOT NULL OR new_value IS NOT NULL THEN ' → ' ELSE '' END || COALESCE(new_value,'') AS details,
                           created_at, id AS sort_id
                    FROM submission_events
                ) ORDER BY created_at DESC, sort_id DESC LIMIT ?
                """,
                (limit,),
            )).fetchall()
        return [dict(r) for r in rows]

    async def analytics(self) -> dict[str, Any]:
        async with self.db.connection() as conn:
            summary = await (await conn.execute(
                """SELECT COUNT(*) total,
                          SUM(CASE WHEN status IN ('confirmed','paid','completed') THEN 1 ELSE 0 END) successful,
                          SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) cancelled,
                          SUM(CASE WHEN status IN ('confirmed','paid','completed') THEN total_amount ELSE 0 END) revenue,
                          AVG(CASE WHEN status IN ('confirmed','paid','completed') AND total_amount>0 THEN total_amount END) avg_check
                   FROM form_submissions"""
            )).fetchone()
            forms = await (await conn.execute(
                """SELECT form_name, COUNT(*) c,
                          SUM(CASE WHEN status IN ('confirmed','paid','completed') THEN total_amount ELSE 0 END) revenue
                   FROM form_submissions GROUP BY form_name ORDER BY c DESC LIMIT 12"""
            )).fetchall()
            months = await (await conn.execute(
                """SELECT substr(created_at,1,7) month, COUNT(*) c,
                          SUM(CASE WHEN status IN ('confirmed','paid','completed') THEN total_amount ELSE 0 END) revenue
                   FROM form_submissions GROUP BY substr(created_at,1,7) ORDER BY month DESC LIMIT 12"""
            )).fetchall()
            statuses = await (await conn.execute(
                "SELECT status, COUNT(*) c FROM form_submissions GROUP BY status"
            )).fetchall()
        result = dict(summary) if summary else {}
        result["forms"] = [dict(r) for r in forms]
        result["months"] = [dict(r) for r in months]
        result["statuses"] = [dict(r) for r in statuses]
        total = int(result.get("total") or 0)
        successful = int(result.get("successful") or 0)
        result["conversion_percent"] = round(successful * 100 / total, 1) if total else 0.0
        return result

    async def create_client_request(self, submission_id: int, chat_id: int, request_type: str, message: str = "") -> int:
        now = utc_now_iso()
        async with self.db.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO client_requests(submission_id,chat_id,request_type,message,status,created_at,updated_at) VALUES(?,?,?,?, 'new', ?, ?)",
                (submission_id, chat_id, request_type[:50], message[:2000], now, now),
            )
            await conn.commit()
            return int(cur.lastrowid)

    async def list_client_requests(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT cr.*, fs.form_name, fs.first_name, fs.last_name FROM client_requests cr JOIN form_submissions fs ON fs.id=cr.submission_id"
        args: list[Any] = []
        if status:
            sql += " WHERE cr.status=?"
            args.append(status)
        sql += " ORDER BY cr.created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 300)))
        async with self.db.connection() as conn:
            rows = await (await conn.execute(sql, tuple(args))).fetchall()
        return [dict(r) for r in rows]
    async def update_client_request_status(self, request_id: int, status: str) -> None:
        if status not in {"new", "in_progress", "done", "rejected"}:
            raise ValueError("status")
        async with self.db.connection() as conn:
            await conn.execute(
                "UPDATE client_requests SET status=?, updated_at=? WHERE id=?",
                (status, utc_now_iso(), request_id),
            )
            await conn.commit()

