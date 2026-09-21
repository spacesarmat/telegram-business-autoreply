from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database, utc_now_iso


def guest_upper_bound(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    raw = str(value).casefold().replace("ё", "е")
    numbers = [int(x) for x in re.findall(r"\d+", raw)]
    if not numbers:
        return 10_000 if "более" in raw else None
    if "более" in raw:
        return numbers[-1] + 1
    return max(numbers)


class V22Service:
    """Backward-compatible v2.2 domain layer.

    Tables are deliberately additive: an existing v2.1.1 SQLite database can be
    opened without rebuilding or losing historical submissions.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def init(self) -> None:
        async with self.db.connection() as conn:
            await conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS addon_resources (
                    addon_id INTEGER NOT NULL, resource_id INTEGER NOT NULL,
                    units_per_item INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(addon_id,resource_id),
                    FOREIGN KEY(addon_id) REFERENCES form_addons(id) ON DELETE CASCADE,
                    FOREIGN KEY(resource_id) REFERENCES resources(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS package_items (
                    package_id INTEGER NOT NULL, item_type TEXT NOT NULL,
                    item_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(package_id,item_type,item_id),
                    FOREIGN KEY(package_id) REFERENCES service_packages(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS promotions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE,
                    name TEXT NOT NULL, discount_type TEXT NOT NULL DEFAULT 'percent',
                    discount_value INTEGER NOT NULL DEFAULT 0, min_amount INTEGER NOT NULL DEFAULT 0,
                    starts_at TEXT, ends_at TEXT, usage_limit INTEGER NOT NULL DEFAULT 0,
                    per_client_limit INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS promotion_uses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, promotion_id INTEGER NOT NULL,
                    submission_id INTEGER NOT NULL, client_key TEXT NOT NULL, discount_amount INTEGER NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(promotion_id,submission_id),
                    FOREIGN KEY(promotion_id) REFERENCES promotions(id) ON DELETE CASCADE,
                    FOREIGN KEY(submission_id) REFERENCES form_submissions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS cancellation_policies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, form_id INTEGER, venue_id INTEGER,
                    hours_before INTEGER NOT NULL DEFAULT 0, refund_percent INTEGER NOT NULL DEFAULT 0,
                    fee_amount INTEGER NOT NULL DEFAULT 0, description TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS refunds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL,
                    payment_transaction_id INTEGER, provider_refund_id TEXT NOT NULL DEFAULT '',
                    amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(submission_id) REFERENCES form_submissions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL,
                    kind TEXT NOT NULL, number TEXT NOT NULL DEFAULT '', file_path TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                    FOREIGN KEY(submission_id) REFERENCES form_submissions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS document_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT NOT NULL,
                    body TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS client_profiles (
                    client_key TEXT PRIMARY KEY, blacklisted INTEGER NOT NULL DEFAULT 0,
                    blacklist_reason TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL DEFAULT '[]',
                    loyalty_points INTEGER NOT NULL DEFAULT 0, loyalty_level TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT 'ru', updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS manager_shifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, manager TEXT NOT NULL, starts_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS important_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER, recipient TEXT NOT NULL,
                    kind TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    due_at TEXT, sent_at TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS client_portal_tokens (
                    submission_id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE, expires_at TEXT,
                    created_at TEXT NOT NULL, FOREIGN KEY(submission_id) REFERENCES form_submissions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'normal', description TEXT NOT NULL,
                    deposit_withheld INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(submission_id) REFERENCES form_submissions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS event_checklists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'before', title TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
                    completed_at TEXT, created_at TEXT NOT NULL,
                    FOREIGN KEY(submission_id) REFERENCES form_submissions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS event_photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL,
                    phase TEXT NOT NULL, file_path TEXT NOT NULL, telegram_file_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, FOREIGN KEY(submission_id) REFERENCES form_submissions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS rate_limit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, actor_key TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rate_limit ON rate_limit_events(scope,actor_key,created_at);
                CREATE TABLE IF NOT EXISTS error_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, level TEXT NOT NULL,
                    message TEXT NOT NULL, context_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
                );
                """
            )
            await self._add_columns(conn, "venues", {
                "capacity": "INTEGER NOT NULL DEFAULT 0",
                "working_hours_json": "TEXT NOT NULL DEFAULT '{}'",
                "buffer_before_minutes": "INTEGER NOT NULL DEFAULT 0",
                "buffer_after_minutes": "INTEGER NOT NULL DEFAULT 0",
                "booking_rules_json": "TEXT NOT NULL DEFAULT '{}'",
                "equipment_description": "TEXT NOT NULL DEFAULT ''",
            })
            await self._add_columns(conn, "form_questions", {
                "condition_question_id": "INTEGER",
                "condition_operator": "TEXT NOT NULL DEFAULT ''",
                "condition_value": "TEXT NOT NULL DEFAULT ''",
            })
            await self._add_columns(conn, "form_submissions", {
                "promotion_id": "INTEGER",
                "discount_amount": "INTEGER NOT NULL DEFAULT 0",
                "prepayment_due_at": "TEXT",
                "cancelled_at": "TEXT",
                "cancellation_reason": "TEXT NOT NULL DEFAULT ''",
                "locale": "TEXT NOT NULL DEFAULT 'ru'",
            })
            # The venue must be selected after guest count so capacity can be
            # enforced. Existing custom forms without a guest question are left intact.
            await conn.execute(
                """UPDATE form_questions AS venue SET position=(
                       SELECT MAX(guest.position)+1 FROM form_questions AS guest
                       WHERE guest.form_id=venue.form_id AND guest.input_type='guest_count'
                   )
                   WHERE venue.input_type='venue' AND EXISTS(
                       SELECT 1 FROM form_questions AS guest
                       WHERE guest.form_id=venue.form_id AND guest.input_type='guest_count'
                         AND guest.position>=venue.position
                   )"""
            )
            await conn.commit()

        defaults = {
            "staging_mode": os.getenv("STAGING_MODE", "0"), "error_monitoring_enabled": "1",
            "antispam_window_seconds": "20", "antispam_max_messages": "8",
            "hold_rate_window_minutes": "10", "hold_rate_max": "5",
            "prepayment_deadline_hours": "24", "postgres_dsn": os.getenv("POSTGRES_DSN", ""),
            "default_language": "ru", "supported_languages": "ru,en",
        }
        for key, value in defaults.items():
            if await self.db.get_setting(key, None) is None:
                await self.db.set_setting(key, value)

    @staticmethod
    async def _add_columns(conn: Any, table: str, fields: dict[str, str]) -> None:
        rows = await (await conn.execute(f"PRAGMA table_info({table})")).fetchall()
        existing = {str(row["name"]) for row in rows}
        for name, ddl in fields.items():
            if name not in existing:
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    async def suitable_venues(self, form_id: int, guest_answer: str | int | None) -> list[dict[str, Any]]:
        guests = guest_upper_bound(guest_answer)
        async with self.db.connection() as conn:
            rows = await (await conn.execute(
                """SELECT v.* FROM venues v JOIN form_venues fv ON fv.venue_id=v.id
                   WHERE fv.form_id=? AND fv.enabled=1 AND v.enabled=1 ORDER BY v.position,v.id""",
                (form_id,),
            )).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            capacity = max(0, int(item.get("capacity") or 0))
            item["suitable"] = guests is None or capacity == 0 or guests <= capacity
            item["capacity_reason"] = "" if item["suitable"] else f"вместимость до {capacity} гостей"
            result.append(item)
        return result

    @staticmethod
    def question_visible(question: dict[str, Any], answers: dict[str, str]) -> bool:
        source = question.get("condition_question_id")
        if not source:
            return True
        actual = str(answers.get(str(source), "")).strip().casefold()
        expected = str(question.get("condition_value") or "").strip().casefold()
        op = str(question.get("condition_operator") or "equals")
        if op == "not_equals":
            return actual != expected
        if op == "contains":
            return expected in actual
        if op == "not_empty":
            return bool(actual)
        if op in {"gt", "gte", "lt", "lte"}:
            left, right = guest_upper_bound(actual), guest_upper_bound(expected)
            if left is None or right is None:
                return False
            return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]
        return actual == expected

    async def link_addon_resource(self, addon_id: int, resource_id: int, units_per_item: int = 1) -> None:
        async with self.db.connection() as conn:
            await conn.execute(
                """INSERT INTO addon_resources(addon_id,resource_id,units_per_item) VALUES(?,?,?)
                   ON CONFLICT(addon_id,resource_id) DO UPDATE SET units_per_item=excluded.units_per_item""",
                (addon_id, resource_id, max(1, int(units_per_item))),
            )
            await conn.commit()

    async def save_package_items(self, package_id: int, items: list[dict[str, Any]]) -> None:
        allowed = {"addon", "resource"}
        async with self.db.connection() as conn:
            await conn.execute("DELETE FROM package_items WHERE package_id=?", (package_id,))
            for item in items:
                kind = str(item.get("item_type") or "")
                item_id = int(item.get("item_id") or 0)
                if kind not in allowed or item_id <= 0:
                    continue
                await conn.execute(
                    "INSERT INTO package_items(package_id,item_type,item_id,quantity) VALUES(?,?,?,?)",
                    (package_id, kind, item_id, max(1, int(item.get("quantity") or 1))),
                )
            await conn.commit()

    async def create_promotion(
        self, *, code: str, name: str, discount_type: str, discount_value: int,
        min_amount: int = 0, usage_limit: int = 0, per_client_limit: int = 1,
        starts_at: str | None = None, ends_at: str | None = None,
    ) -> int:
        discount_type = discount_type if discount_type in {"percent", "fixed"} else "percent"
        normalized = re.sub(r"[^A-Z0-9_-]", "", code.strip().upper())[:40]
        if not normalized:
            raise ValueError("Некорректный промокод")
        now = utc_now_iso()
        async with self.db.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO promotions(code,name,discount_type,discount_value,min_amount,starts_at,ends_at,usage_limit,per_client_limit,enabled,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
                (normalized, name[:150], discount_type, max(0, int(discount_value)), max(0, int(min_amount)),
                 starts_at, ends_at, max(0, int(usage_limit)), max(0, int(per_client_limit)), now, now),
            )
            await conn.commit()
            return int(cur.lastrowid)

    async def apply_promotion(self, submission_id: int, code: str, client_key: str) -> int:
        now = utc_now_iso()
        async with self.db.connection() as conn:
            promo = await (await conn.execute("SELECT * FROM promotions WHERE code=? AND enabled=1", (code.strip().upper(),))).fetchone()
            submission = await (await conn.execute("SELECT total_amount FROM form_submissions WHERE id=?", (submission_id,))).fetchone()
            if not promo or not submission:
                raise ValueError("Промокод не найден")
            if promo["starts_at"] and str(promo["starts_at"]) > now:
                raise ValueError("Промокод ещё не действует")
            if promo["ends_at"] and str(promo["ends_at"]) < now:
                raise ValueError("Срок промокода истёк")
            total_uses = await (await conn.execute("SELECT COUNT(*) c FROM promotion_uses WHERE promotion_id=?", (promo["id"],))).fetchone()
            client_uses = await (await conn.execute("SELECT COUNT(*) c FROM promotion_uses WHERE promotion_id=? AND client_key=?", (promo["id"], client_key))).fetchone()
            if int(promo["usage_limit"] or 0) and int(total_uses["c"] or 0) >= int(promo["usage_limit"]):
                raise ValueError("Лимит промокода исчерпан")
            if int(promo["per_client_limit"] or 0) and int(client_uses["c"] or 0) >= int(promo["per_client_limit"]):
                raise ValueError("Промокод уже использован клиентом")
            amount = max(0, int(submission["total_amount"] or 0))
            if amount < int(promo["min_amount"] or 0):
                raise ValueError("Недостаточная сумма заказа")
            if promo["discount_type"] == "fixed":
                discount = min(amount, int(promo["discount_value"] or 0))
            else:
                discount = min(amount, amount * min(100, int(promo["discount_value"] or 0)) // 100)
            await conn.execute("UPDATE form_submissions SET promotion_id=?,discount_amount=?,total_amount=?,updated_at=? WHERE id=?", (promo["id"], discount, amount - discount, now, submission_id))
            await conn.execute("INSERT INTO promotion_uses(promotion_id,submission_id,client_key,discount_amount,created_at) VALUES(?,?,?,?,?)", (promo["id"], submission_id, client_key, discount, now))
            await conn.commit()
            return discount

    async def cancellation_quote(self, submission_id: int, hours_before: int) -> dict[str, int]:
        async with self.db.connection() as conn:
            submission = await (await conn.execute("SELECT * FROM form_submissions WHERE id=?", (submission_id,))).fetchone()
            if not submission:
                raise ValueError("Заявка не найдена")
            policy = await (await conn.execute(
                """SELECT * FROM cancellation_policies WHERE enabled=1
                   AND (form_id IS NULL OR form_id=?) AND (venue_id IS NULL OR venue_id=?)
                   AND hours_before<=? ORDER BY hours_before DESC LIMIT 1""",
                (submission["form_id"], submission["venue_id"], max(0, hours_before)),
            )).fetchone()
        paid = max(0, int(submission["prepayment_amount"] or 0))
        percent = max(0, min(100, int(policy["refund_percent"] or 0))) if policy else 0
        fee = max(0, int(policy["fee_amount"] or 0)) if policy else 0
        refund = max(0, paid * percent // 100 - fee)
        return {"paid": paid, "refund": min(paid, refund), "fee": min(paid, paid - refund)}

    async def set_client_profile(
        self, client_key: str, *, blacklisted: bool = False, blacklist_reason: str = "",
        tags: list[str] | None = None, loyalty_points: int = 0, loyalty_level: str = "",
        language: str = "ru",
    ) -> None:
        clean_tags = list(dict.fromkeys(str(x).strip()[:40] for x in (tags or []) if str(x).strip()))[:30]
        async with self.db.connection() as conn:
            await conn.execute(
                """INSERT INTO client_profiles(client_key,blacklisted,blacklist_reason,tags_json,loyalty_points,loyalty_level,language,updated_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(client_key) DO UPDATE SET
                   blacklisted=excluded.blacklisted,blacklist_reason=excluded.blacklist_reason,tags_json=excluded.tags_json,
                   loyalty_points=excluded.loyalty_points,loyalty_level=excluded.loyalty_level,language=excluded.language,updated_at=excluded.updated_at""",
                (client_key, 1 if blacklisted else 0, blacklist_reason[:1000], json.dumps(clean_tags, ensure_ascii=False),
                 max(0, int(loyalty_points)), loyalty_level[:50], language[:10], utc_now_iso()),
            )
            await conn.commit()

    async def client_blocked(self, client_key: str) -> tuple[bool, str]:
        async with self.db.connection() as conn:
            row = await (await conn.execute("SELECT blacklisted,blacklist_reason FROM client_profiles WHERE client_key=?", (client_key,))).fetchone()
        return (bool(row and row["blacklisted"]), str(row["blacklist_reason"] or "") if row else "")

    async def current_manager(self, at: str | None = None) -> str | None:
        moment = at or utc_now_iso()
        async with self.db.connection() as conn:
            row = await (await conn.execute(
                "SELECT manager FROM manager_shifts WHERE starts_at<=? AND ends_at>? ORDER BY starts_at DESC LIMIT 1",
                (moment, moment),
            )).fetchone()
        return str(row["manager"]) if row else None

    async def add_incident(
        self, submission_id: int, description: str, *, severity: str = "normal",
        deposit_withheld: int = 0,
    ) -> int:
        now = utc_now_iso()
        async with self.db.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO incidents(submission_id,severity,description,deposit_withheld,status,created_at,updated_at) VALUES(?,?,?,?, 'open',?,?)",
                (submission_id, severity[:20], description[:4000], max(0, int(deposit_withheld)), now, now),
            )
            if deposit_withheld:
                await conn.execute(
                    "UPDATE form_submissions SET deposit_status='withheld',updated_at=? WHERE id=?",
                    (now, submission_id),
                )
            await conn.commit()
            return int(cur.lastrowid)

    async def add_checklist_item(self, submission_id: int, title: str, phase: str = "before") -> int:
        phase = phase if phase in {"before", "during", "after"} else "before"
        async with self.db.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO event_checklists(submission_id,phase,title,created_at) VALUES(?,?,?,?)",
                (submission_id, phase, title[:500], utc_now_iso()),
            )
            await conn.commit()
            return int(cur.lastrowid)

    async def complete_checklist_item(self, item_id: int, completed: bool = True) -> None:
        async with self.db.connection() as conn:
            await conn.execute(
                "UPDATE event_checklists SET completed=?,completed_at=? WHERE id=?",
                (1 if completed else 0, utc_now_iso() if completed else None, item_id),
            )
            await conn.commit()

    async def add_event_photo(
        self, submission_id: int, phase: str, file_path: str, telegram_file_id: str = ""
    ) -> int:
        if phase not in {"before", "after"}:
            raise ValueError("phase должен быть before или after")
        async with self.db.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO event_photos(submission_id,phase,file_path,telegram_file_id,created_at) VALUES(?,?,?,?,?)",
                (submission_id, phase, file_path[:1000], telegram_file_id[:300], utc_now_iso()),
            )
            await conn.commit()
            return int(cur.lastrowid)

    async def queue_notification(
        self, recipient: str, kind: str, message: str, *, submission_id: int | None = None,
        due_at: str | None = None,
    ) -> int:
        async with self.db.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO important_notifications(submission_id,recipient,kind,message,due_at,created_at) VALUES(?,?,?,?,?,?)",
                (submission_id, recipient[:150], kind[:50], message[:4000], due_at, utc_now_iso()),
            )
            await conn.commit()
            return int(cur.lastrowid)

    async def create_document(self, submission_id: int, kind: str, payload: dict[str, Any]) -> int:
        if kind not in {"invoice", "receipt", "contract"}:
            raise ValueError("Тип документа не поддерживается")
        async with self.db.connection() as conn:
            template = await (await conn.execute("SELECT body FROM document_templates WHERE kind=? AND enabled=1 ORDER BY id DESC LIMIT 1", (kind,))).fetchone()
            body = str(template["body"] if template else "")
            for key, value in payload.items():
                body = body.replace("{" + str(key) + "}", str(value))
            number = f"{submission_id}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
            cur = await conn.execute("INSERT INTO documents(submission_id,kind,number,payload_json,created_at) VALUES(?,?,?,?,?)", (submission_id, kind, number, json.dumps({**payload, "rendered": body}, ensure_ascii=False), utc_now_iso()))
            await conn.commit()
            return int(cur.lastrowid)

    async def addon_available_quantity(self, addon_id: int, segments: list[tuple[str, str, str]]) -> int | None:
        async with self.db.connection() as conn:
            links = await (await conn.execute(
                """SELECT ar.*,r.quantity FROM addon_resources ar JOIN resources r ON r.id=ar.resource_id
                   WHERE ar.addon_id=? AND r.enabled=1""", (addon_id,),
            )).fetchall()
            if not links:
                return None
            limits: list[int] = []
            for link in links:
                used = 0
                allocations = await (await conn.execute(
                    """SELECT ra.quantity,ab.date_iso,ab.start_time,ab.end_time FROM resource_allocations ra
                       JOIN availability_blocks ab ON ab.source_submission_id=ra.submission_id
                       WHERE ra.resource_id=?""", (int(link["resource_id"]),),
                )).fetchall()
                for allocation in allocations:
                    for date_iso, start, end in segments:
                        if allocation["date_iso"] == date_iso and str(allocation["start_time"] or "00:00") < end and str(allocation["end_time"] or "24:00") > start:
                            used += int(allocation["quantity"] or 0)
                            break
                free = max(0, int(link["quantity"] or 0) - used)
                limits.append(free // max(1, int(link["units_per_item"] or 1)))
            return min(limits) if limits else None

    async def allocate_addon_resources(
        self, submission_id: int, selections: list[dict[str, Any]]
    ) -> None:
        async with self.db.connection() as conn:
            for addon in selections:
                addon_id = int(addon.get("id") or 0)
                quantity = max(1, int(addon.get("quantity") or 1))
                links = await (await conn.execute(
                    "SELECT resource_id,units_per_item FROM addon_resources WHERE addon_id=?", (addon_id,)
                )).fetchall()
                for link in links:
                    required = quantity * max(1, int(link["units_per_item"] or 1))
                    await conn.execute(
                        """INSERT INTO resource_allocations(submission_id,resource_id,quantity,created_at)
                           VALUES(?,?,?,?) ON CONFLICT(submission_id,resource_id) DO UPDATE SET quantity=excluded.quantity""",
                        (submission_id, int(link["resource_id"]), required, utc_now_iso()),
                    )
            await conn.commit()

    async def rate_allowed(self, scope: str, actor_key: str, *, limit: int, window_seconds: int) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(1, window_seconds))).isoformat()
        now = utc_now_iso()
        async with self.db.connection() as conn:
            await conn.execute("DELETE FROM rate_limit_events WHERE created_at<?", (cutoff,))
            count = await (await conn.execute(
                "SELECT COUNT(*) c FROM rate_limit_events WHERE scope=? AND actor_key=? AND created_at>=?",
                (scope, actor_key, cutoff),
            )).fetchone()
            allowed = int(count["c"] or 0) < max(1, limit)
            if allowed:
                await conn.execute("INSERT INTO rate_limit_events(scope,actor_key,created_at) VALUES(?,?,?)", (scope, actor_key, now))
            await conn.commit()
        return allowed

    async def portal_token(self, submission_id: int, ttl_days: int = 90) -> str:
        async with self.db.connection() as conn:
            row = await (await conn.execute("SELECT token FROM client_portal_tokens WHERE submission_id=?", (submission_id,))).fetchone()
            if row:
                return str(row["token"])
            token = secrets.token_urlsafe(24)
            expires = (datetime.now(timezone.utc) + timedelta(days=max(1, ttl_days))).isoformat()
            await conn.execute("INSERT INTO client_portal_tokens(submission_id,token,expires_at,created_at) VALUES(?,?,?,?)", (submission_id, token, expires, utc_now_iso()))
            await conn.commit()
            return token

    async def venue_analytics(self, date_from: str, date_to: str) -> list[dict[str, Any]]:
        async with self.db.connection() as conn:
            rows = await (await conn.execute(
                """SELECT v.id venue_id,v.name venue_name,COUNT(DISTINCT fs.id) bookings,
                   COALESCE(SUM(fs.total_amount),0) revenue,
                   COALESCE(SUM((julianday(ab.date_iso||' '||CASE WHEN ab.end_time='24:00' THEN '23:59' ELSE ab.end_time END)-julianday(ab.date_iso||' '||COALESCE(ab.start_time,'00:00')))*24),0) booked_hours
                   FROM venues v LEFT JOIN form_submissions fs ON fs.venue_id=v.id AND fs.status IN ('confirmed','paid','completed')
                   LEFT JOIN availability_blocks ab ON ab.source_submission_id=fs.id AND ab.date_iso BETWEEN ? AND ?
                   GROUP BY v.id,v.name ORDER BY v.position,v.id""", (date_from, date_to),
            )).fetchall()
        return [dict(row) for row in rows]

    async def occupancy_heatmap(self, date_from: str, date_to: str) -> list[dict[str, Any]]:
        async with self.db.connection() as conn:
            rows = await (await conn.execute(
                """SELECT venue_id,CAST(strftime('%w',date_iso) AS INTEGER) weekday,
                   CAST(substr(COALESCE(start_time,'00:00'),1,2) AS INTEGER) hour,COUNT(*) bookings
                   FROM availability_blocks WHERE date_iso BETWEEN ? AND ? AND source_submission_id IS NOT NULL
                   GROUP BY venue_id,weekday,hour ORDER BY venue_id,weekday,hour""", (date_from, date_to),
            )).fetchall()
        return [dict(row) for row in rows]

    async def record_error(self, source: str, message: str, context: dict[str, Any] | None = None) -> None:
        async with self.db.connection() as conn:
            await conn.execute("INSERT INTO error_events(source,level,message,context_json,created_at) VALUES(?,'error',?,?,?)", (source[:100], message[:4000], json.dumps(context or {}, ensure_ascii=False)[:10000], utc_now_iso()))
            await conn.commit()
