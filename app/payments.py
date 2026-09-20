from __future__ import annotations

import base64
import secrets
from decimal import Decimal, InvalidOperation
from typing import Any

from aiohttp import ClientSession, ClientTimeout

from .db import Database, utc_now_iso


class PaymentService:
    """Optional online-payment layer.

    `template` keeps the old external-link workflow. `yookassa` creates a real
    redirect payment and verifies incoming notifications against YooKassa API
    before changing CRM amounts.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def init(self) -> None:
        async with self.db.connection() as conn:
            await conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS payment_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    provider_payment_id TEXT NOT NULL,
                    amount INTEGER NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'RUB',
                    status TEXT NOT NULL DEFAULT 'pending',
                    confirmation_url TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(provider, provider_payment_id),
                    FOREIGN KEY(submission_id) REFERENCES form_submissions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_payment_transactions_submission
                    ON payment_transactions(submission_id, created_at);
                """
            )
            await conn.commit()

    async def _credentials(self) -> tuple[str, str]:
        shop_id = str(await self.db.get_setting("yookassa_shop_id", "") or "").strip()
        secret = str(await self.db.get_setting("yookassa_secret_key", "") or "").strip()
        return shop_id, secret

    @staticmethod
    def _auth_header(shop_id: str, secret: str) -> str:
        return "Basic " + base64.b64encode(f"{shop_id}:{secret}".encode()).decode()

    @staticmethod
    def _rubles(value: Any) -> int:
        try:
            return max(0, int(Decimal(str(value))))
        except (InvalidOperation, TypeError, ValueError):
            return 0

    async def create_payment_url(self, submission: dict[str, Any], amount: int, return_url: str) -> tuple[str | None, str]:
        provider = str(await self.db.get_setting("payment_provider", "off") or "off").strip().lower()
        if provider in {"", "off"}:
            return None, "Онлайн-эквайринг выключен"
        if provider == "template":
            template = str(await self.db.get_setting("payment_link_template", "") or "").strip()
            if not template:
                return None, "Не задан шаблон ссылки оплаты"
            values = {"id": str(submission.get("id") or ""), "amount": str(max(0, int(amount)))}
            url = template
            for key, value in values.items():
                url = url.replace("{" + key + "}", value)
            return url, "template"
        if provider == "yookassa":
            shop_id, secret = await self._credentials()
            if not shop_id or not secret:
                return None, "Не настроены shop_id / secret_key ЮKassa"
            if not return_url.startswith("https://"):
                return None, "Return URL ЮKassa должен быть публичным HTTPS-адресом"
            rubles = max(1, int(amount))
            payload = {
                "amount": {"value": f"{rubles:.2f}", "currency": "RUB"},
                "capture": True,
                "confirmation": {"type": "redirect", "return_url": return_url},
                "description": f"Предоплата по заявке №{submission.get('id')}",
                "metadata": {"submission_id": str(submission.get("id") or "")},
            }
            headers = {
                "Authorization": self._auth_header(shop_id, secret),
                "Idempotence-Key": secrets.token_hex(16),
                "Content-Type": "application/json",
            }
            try:
                async with ClientSession(timeout=ClientTimeout(total=12)) as session:
                    async with session.post("https://api.yookassa.ru/v3/payments", json=payload, headers=headers) as resp:
                        data = await resp.json(content_type=None)
                        if resp.status >= 300:
                            return None, f"ЮKassa HTTP {resp.status}: {str(data)[:300]}"
            except Exception as exc:
                return None, f"Ошибка ЮKassa: {exc}"
            confirmation = data.get("confirmation") if isinstance(data, dict) else None
            url = confirmation.get("confirmation_url") if isinstance(confirmation, dict) else None
            payment_id = str(data.get("id") or "") if isinstance(data, dict) else ""
            if not url or not payment_id:
                return None, "ЮKassa не вернула идентификатор/confirmation_url"
            now = utc_now_iso()
            import json
            async with self.db.connection() as conn:
                await conn.execute(
                    """INSERT INTO payment_transactions(
                        submission_id,provider,provider_payment_id,amount,currency,status,
                        confirmation_url,raw_json,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(provider,provider_payment_id) DO UPDATE SET
                        status=excluded.status,confirmation_url=excluded.confirmation_url,
                        raw_json=excluded.raw_json,updated_at=excluded.updated_at""",
                    (
                        int(submission.get("id") or 0), "yookassa", payment_id, rubles, "RUB",
                        str(data.get("status") or "pending"), str(url),
                        json.dumps(data, ensure_ascii=False)[:20000], now, now,
                    ),
                )
                await conn.commit()
            return str(url), payment_id
        return None, f"Неизвестный payment_provider: {provider}"

    async def _fetch_yookassa_payment(self, payment_id: str) -> tuple[dict[str, Any] | None, str]:
        shop_id, secret = await self._credentials()
        if not shop_id or not secret:
            return None, "ЮKassa не настроена"
        headers = {"Authorization": self._auth_header(shop_id, secret)}
        try:
            async with ClientSession(timeout=ClientTimeout(total=12)) as session:
                async with session.get(f"https://api.yookassa.ru/v3/payments/{payment_id}", headers=headers) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status >= 300:
                        return None, f"ЮKassa HTTP {resp.status}"
                    return data if isinstance(data, dict) else None, "ok"
        except Exception as exc:
            return None, f"Ошибка проверки ЮKassa: {exc}"

    async def handle_yookassa_notification(self, payload: dict[str, Any]) -> tuple[bool, str, int | None]:
        obj = payload.get("object") if isinstance(payload, dict) else None
        payment_id = str(obj.get("id") or "") if isinstance(obj, dict) else ""
        if not payment_id:
            return False, "Нет payment id", None
        # Do not trust webhook body. Re-read payment from provider API.
        verified, error = await self._fetch_yookassa_payment(payment_id)
        if not verified:
            return False, error, None
        metadata = verified.get("metadata") if isinstance(verified.get("metadata"), dict) else {}
        try:
            submission_id = int(metadata.get("submission_id") or 0)
        except (TypeError, ValueError):
            submission_id = 0
        if not submission_id:
            return False, "В metadata нет submission_id", None
        status = str(verified.get("status") or "unknown")
        amount_data = verified.get("amount") if isinstance(verified.get("amount"), dict) else {}
        amount = self._rubles(amount_data.get("value"))
        import json
        now = utc_now_iso()
        async with self.db.connection() as conn:
            await conn.execute(
                """INSERT INTO payment_transactions(
                    submission_id,provider,provider_payment_id,amount,currency,status,
                    confirmation_url,raw_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(provider,provider_payment_id) DO UPDATE SET
                    submission_id=excluded.submission_id,amount=excluded.amount,
                    status=excluded.status,raw_json=excluded.raw_json,updated_at=excluded.updated_at""",
                (
                    submission_id, "yookassa", payment_id, amount,
                    str(amount_data.get("currency") or "RUB"), status, "",
                    json.dumps(verified, ensure_ascii=False)[:20000], now, now,
                ),
            )
            if status == "succeeded":
                row = await (await conn.execute(
                    "SELECT total_amount,prepayment_amount,status FROM form_submissions WHERE id=?",
                    (submission_id,),
                )).fetchone()
                if row:
                    old_pre = int(row["prepayment_amount"] or 0)
                    new_pre = max(old_pre, amount)
                    # Status is intentionally changed by the main CRM status path,
                    # so booking occupancy and Telegram notifications stay consistent.
                    await conn.execute(
                        "UPDATE form_submissions SET prepayment_amount=?,updated_at=? WHERE id=?",
                        (new_pre, now, submission_id),
                    )
                    await conn.execute(
                        "INSERT INTO submission_events(submission_id,event_type,old_value,new_value,created_at) VALUES(?,'payment_succeeded',?,?,?)",
                        (submission_id, str(old_pre), str(new_pre), now),
                    )
            await conn.commit()
        return True, status, submission_id

    async def list_transactions(self, submission_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM payment_transactions"
        args: list[Any] = []
        if submission_id is not None:
            sql += " WHERE submission_id=?"
            args.append(submission_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        async with self.db.connection() as conn:
            rows = await (await conn.execute(sql, tuple(args))).fetchall()
        return [dict(r) for r in rows]
