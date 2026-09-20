from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .db import Database

logger = logging.getLogger(__name__)

BookingStartResolver = Callable[[dict], Awaitable[datetime | None]]


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _parse_hours(raw: str | None) -> list[int]:
    result: list[int] = []
    for token in (raw or "").replace(";", ",").replace("\n", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError:
            continue
        if 1 <= value <= 24 * 90 and value not in result:
            result.append(value)
    return sorted(result, reverse=True)[:12]


def _money(value: int | str | None, currency: str) -> str:
    try:
        amount = max(0, int(value or 0))
    except (TypeError, ValueError):
        amount = 0
    return f"{amount:,}".replace(",", " ") + f" {currency}"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class ReminderService:
    def __init__(
        self,
        *,
        db: Database,
        bot: Bot,
        timezone: ZoneInfo,
        admin_ids: set[int],
        resolve_booking_start: BookingStartResolver,
    ) -> None:
        self.db = db
        self.bot = bot
        self.timezone = timezone
        self.admin_ids = set(admin_ids)
        self.resolve_booking_start = resolve_booking_start
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._scan_lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="booking-reminder-loop")

    async def close(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_once(self) -> dict[str, int]:
        async with self._scan_lock:
            if (await self.db.get_setting("reminders_enabled", "0")) != "1":
                return {"checked": 0, "sent": 0, "failed": 0, "missed": 0}

            client_hours = _parse_hours(await self.db.get_setting("reminder_client_hours", "168,24,3"))
            admin_hours = _parse_hours(await self.db.get_setting("reminder_admin_hours", "168,24,3"))
            try:
                grace_hours = int(await self.db.get_setting("reminder_grace_hours", "6") or 6)
            except ValueError:
                grace_hours = 6
            grace = timedelta(hours=max(1, min(grace_hours, 72)))
            client_template = await self.db.get_setting("reminder_client_template", "") or ""
            admin_template = await self.db.get_setting("reminder_admin_template", "") or ""
            currency = str(await self.db.get_setting("crm_currency", "₽") or "₽")

            now_utc = datetime.now(timezone.utc)
            result = {"checked": 0, "sent": 0, "failed": 0, "missed": 0}
            submissions = await self.db.list_booking_submissions(limit=2000)
            for submission in submissions:
                try:
                    start_local = await self.resolve_booking_start(submission)
                except Exception:
                    logger.exception("Could not resolve booking start for submission %s", submission.get("id"))
                    continue
                if not start_local:
                    continue
                if start_local.tzinfo is None:
                    start_local = start_local.replace(tzinfo=self.timezone)
                start_utc = start_local.astimezone(timezone.utc)
                if start_utc <= now_utc:
                    continue
                values = self._template_values(submission, start_local, currency)

                for hours_before in client_hours:
                    due_utc = start_utc - timedelta(hours=hours_before)
                    if due_utc > now_utc:
                        continue
                    result["checked"] += 1
                    status = await self._deliver(
                        submission=submission,
                        recipient_key="client",
                        hours_before=hours_before,
                        due_utc=due_utc,
                        now_utc=now_utc,
                        grace=grace,
                        text=self._format_template(client_template, values, hours_before),
                    )
                    if status in result:
                        result[status] += 1

                for hours_before in admin_hours:
                    due_utc = start_utc - timedelta(hours=hours_before)
                    if due_utc > now_utc:
                        continue
                    text = self._format_template(admin_template, values, hours_before)
                    for admin_id in sorted(self.admin_ids):
                        result["checked"] += 1
                        status = await self._deliver(
                            submission=submission,
                            recipient_key=f"admin:{admin_id}",
                            hours_before=hours_before,
                            due_utc=due_utc,
                            now_utc=now_utc,
                            grace=grace,
                            text=text,
                        )
                        if status in result:
                            result[status] += 1
            return result

    def _template_values(self, submission: dict, start_local: datetime, currency: str) -> dict[str, str]:
        client = " ".join(
            str(x).strip()
            for x in (submission.get("first_name"), submission.get("last_name"))
            if x
        )
        if not client:
            client = ("@" + str(submission.get("username"))) if submission.get("username") else "Клиент"
        amount = max(0, int(submission.get("total_amount") or 0))
        prepayment = max(0, int(submission.get("prepayment_amount") or 0))
        return {
            "id": str(submission.get("id") or ""),
            "form": str(submission.get("form_name") or "Заявка"),
            "client": client,
            "username": ("@" + str(submission.get("username"))) if submission.get("username") else "—",
            "date": start_local.strftime("%d.%m.%Y"),
            "time": start_local.strftime("%H:%M"),
            "amount": _money(amount, currency),
            "prepayment": _money(prepayment, currency),
            "balance": _money(max(0, amount - prepayment), currency),
        }

    @staticmethod
    def _format_template(template: str, values: dict[str, str], hours_before: int) -> str:
        mapping = _SafeDict(values)
        mapping["hours_before"] = str(hours_before)
        try:
            text = (template or "").format_map(mapping).strip()
        except (ValueError, KeyError):
            text = (template or "").strip()
        return text[:4096]

    async def _deliver(
        self,
        *,
        submission: dict,
        recipient_key: str,
        hours_before: int,
        due_utc: datetime,
        now_utc: datetime,
        grace: timedelta,
        text: str,
    ) -> str | None:
        due_at = due_utc.isoformat()
        existing = await self.db.get_reminder_delivery(
            int(submission["id"]), recipient_key, hours_before, due_at
        )
        if existing:
            status = str(existing.get("status") or "")
            if status in {"sent", "missed"}:
                return None
            attempts = int(existing.get("attempts") or 0)
            if attempts >= 5:
                return None
            last_attempt = _parse_iso(existing.get("last_attempt_at"))
            if last_attempt and now_utc - last_attempt.astimezone(timezone.utc) < timedelta(minutes=15):
                return None

        if now_utc - due_utc > grace:
            await self.db.record_reminder_delivery(
                int(submission["id"]), recipient_key, hours_before, due_at, "missed",
                "Пропущено: бот был выключен или напоминания включили слишком поздно",
            )
            return "missed"

        if not text:
            await self.db.record_reminder_delivery(
                int(submission["id"]), recipient_key, hours_before, due_at, "failed",
                "Пустой шаблон напоминания",
            )
            return "failed"

        try:
            if recipient_key == "client":
                chat_id = int(submission.get("chat_id") or 0)
                if not chat_id:
                    raise RuntimeError("У заявки нет chat_id")
                connection_id = submission.get("business_connection_id")
                if not connection_id:
                    connection = await self.db.latest_business_connection()
                    connection_id = connection.get("id") if connection and connection.get("enabled") else None
                if not connection_id:
                    raise RuntimeError("Нет активного Telegram Business connection")
                await self.bot.send_message(
                    chat_id=chat_id,
                    business_connection_id=str(connection_id),
                    text=text,
                    parse_mode=None,
                )
            else:
                admin_id = int(recipient_key.split(":", 1)[1])
                await self.bot.send_message(chat_id=admin_id, text=text, parse_mode=None)
        except (TelegramAPIError, RuntimeError, ValueError) as exc:
            logger.warning(
                "Reminder failed: submission=%s recipient=%s hours=%s error=%s",
                submission.get("id"), recipient_key, hours_before, exc,
            )
            await self.db.record_reminder_delivery(
                int(submission["id"]), recipient_key, hours_before, due_at, "failed", str(exc)
            )
            return "failed"
        except Exception as exc:
            logger.exception("Unexpected reminder error")
            await self.db.record_reminder_delivery(
                int(submission["id"]), recipient_key, hours_before, due_at, "failed", str(exc)
            )
            return "failed"

        await self.db.record_reminder_delivery(
            int(submission["id"]), recipient_key, hours_before, due_at, "sent", None
        )
        return "sent"

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Reminder scan failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
