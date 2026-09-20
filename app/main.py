from __future__ import annotations

import asyncio
import calendar
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BusinessConnection, CallbackQuery, Message

from .config import Settings, load_settings
from .db import DEFAULT_STATUS_TEMPLATES, Database, utc_now_iso
from .keyboards import (
    admin_addon_delete_confirm,
    admin_addon_edit,
    admin_addons_list,
    admin_availability,
    admin_button_edit,
    admin_buttons_list,
    admin_form_bindings,
    admin_form_delete_confirm,
    admin_form_edit,
    admin_form_questions,
    admin_forms_list,
    admin_main,
    admin_pricing_form,
    admin_pricing_forms,
    admin_question_delete_confirm,
    admin_question_edit,
    admin_question_type,
    admin_submission_card,
    admin_status_template_edit,
    admin_status_templates,
    admin_submissions_list,
    calendar_keyboard,
    delete_confirm,
    end_time_slots_keyboard,
    form_addons,
    form_confirmation,
    form_question_nav,
    guest_count_keyboard,
    public_menu,
    time_slots_keyboard,
)
from .states import AdminStates
from .web_admin import start_web_admin

logger = logging.getLogger(__name__)

settings: Settings = load_settings()
APP_TIMEZONE = ZoneInfo(settings.timezone_name)
db = Database(settings.database_path)
router = Router(name="main")


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in settings.admin_ids


def local_now() -> datetime:
    """Current time in the configured business/account timezone."""
    return datetime.now(APP_TIMEZONE)


def local_today():
    return local_now().date()


def _as_local_datetime(value: str | None) -> datetime | None:
    """Convert an ISO timestamp stored in UTC to the configured local timezone.

    Old naive values are treated as UTC because all historical database writes in
    this project used utc_now_iso().
    """
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(APP_TIMEZONE)


def _format_local_timestamp(value: str | None, *, with_seconds: bool = False) -> str:
    parsed = _as_local_datetime(value)
    if not parsed:
        return "—"
    fmt = "%d.%m.%Y %H:%M:%S" if with_seconds else "%d.%m.%Y %H:%M"
    return parsed.strftime(fmt)


def _localize_submission_dates(items: list[dict]) -> list[dict]:
    result: list[dict] = []
    for item in items:
        copy = dict(item)
        parsed = _as_local_datetime(copy.get("created_at"))
        copy["created_at_local"] = parsed.strftime("%Y-%m-%d") if parsed else ""
        result.append(copy)
    return result


async def get_autoresponder_enabled() -> bool:
    return (await db.get_setting("autoresponder_enabled", "1")) == "1"


def _normalize_menu_trigger(value: str) -> str:
    value = " ".join(value.strip().casefold().split())
    # Telegram may present a slash command as /menu@BotUsername.
    if value.startswith("/") and " " not in value and "@" in value:
        value = value.split("@", 1)[0]
    return value


def _parse_menu_triggers(raw: str | None) -> list[str]:
    raw = raw or ""
    values: list[str] = []
    seen: set[str] = set()
    for line in raw.replace(";", "\n").splitlines():
        item = _normalize_menu_trigger(line)
        if item and item not in seen:
            seen.add(item)
            values.append(item)
    return values


async def get_menu_triggers() -> list[str]:
    return _parse_menu_triggers(await db.get_setting("menu_triggers", "/menu\nменю\nзаявка"))


async def is_menu_trigger(text: str | None) -> bool:
    if not text:
        return False
    return _normalize_menu_trigger(text) in set(await get_menu_triggers())


async def _show_public_menu(
    bot: Bot,
    *,
    chat_id: int,
    business_connection_id: str,
    text: str = "Выберите, что вас интересует:",
    edit_message_id: int | None = None,
) -> int | None:
    columns = int(await db.get_setting("menu_columns", "1") or 1)
    buttons = await db.list_buttons(enabled_only=True)
    markup = public_menu(buttons, columns)

    if edit_message_id is not None:
        try:
            edited = await bot.edit_message_text(
                chat_id=chat_id,
                message_id=edit_message_id,
                business_connection_id=business_connection_id,
                text=text,
                parse_mode=None,
                reply_markup=markup,
            )
            if isinstance(edited, Message):
                return int(edited.message_id)
            return edit_message_id
        except TelegramAPIError:
            logger.warning("Не удалось превратить сообщение формы в главное меню; отправляю новое")

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            business_connection_id=business_connection_id,
            text=text,
            parse_mode=None,
            reply_markup=markup,
        )
        return int(sent.message_id)
    except TelegramAPIError:
        logger.exception("Не удалось показать главное меню в chat_id=%s", chat_id)
        return None


async def _delete_incoming_business_message(
    bot: Bot, *, business_connection_id: str, message_id: int, allowed: bool
) -> None:
    if not allowed:
        return
    try:
        await bot.delete_business_messages(
            business_connection_id=business_connection_id,
            message_ids=[message_id],
        )
    except TelegramAPIError:
        logger.warning("Не удалось удалить служебное сообщение вызова меню %s", message_id)


async def render_admin_button(button: dict) -> tuple[str, object]:
    status = "включена" if button["enabled"] else "выключена"
    bound_form = await db.get_bound_form(int(button["id"]))
    if bound_form:
        action = f"📝 запускает форму «{html.escape(bound_form['name'])}»"
        response_note = "Текст ниже используется только если форма будет отвязана/выключена."
    else:
        action = "💬 отправляет обычный текстовый ответ"
        response_note = ""
    text = (
        f"<b>{html.escape(button['title'])}</b>\n\n"
        f"Позиция: {button['position']}\n"
        f"Статус: {status}\n"
        f"Действие: {action}\n\n"
        f"<b>Ответ:</b>\n{html.escape(button['response'])}"
    )
    if response_note:
        text += f"\n\n<i>{response_note}</i>"
    return text, admin_button_edit(button)


QUESTION_TYPE_NAMES = {
    "text": "⌨️ Текст",
    "date": "📅 Дата",
    "time": "🕐 Время",
    "contact": "📱 Контакт",
    "guest_count": "👥 Количество гостей",
}


def _question_input_type(question: dict) -> str:
    value = str(question.get("input_type") or "text")
    return value if value in QUESTION_TYPE_NAMES else "text"


GUEST_COUNT_OPTIONS = (
    "до 50",
    "от 50 до 100",
    "от 100 до 150",
    "от 150 до 250",
    "от 250 до 400",
    "более 400",
)


def _normalize_guest_count_answer(value: str | None) -> str | None:
    if not value:
        return None
    raw = " ".join(str(value).strip().casefold().split())
    for option in GUEST_COUNT_OPTIONS:
        if raw == option.casefold():
            return option
    match = re.fullmatch(r"(\d{1,5})(?:\s*(?:гост(?:ей|я|ь)|чел(?:овек)?\.?))?", raw)
    if not match:
        return None
    count = int(match.group(1))
    if count <= 0:
        return None
    if count < 50:
        return GUEST_COUNT_OPTIONS[0]
    if count < 100:
        return GUEST_COUNT_OPTIONS[1]
    if count < 150:
        return GUEST_COUNT_OPTIONS[2]
    if count < 250:
        return GUEST_COUNT_OPTIONS[3]
    if count <= 400:
        return GUEST_COUNT_OPTIONS[4]
    return GUEST_COUNT_OPTIONS[5]


def _parse_date_answer(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            return parsed.strftime("%d.%m.%Y")
        except ValueError:
            continue
    return None


def _date_from_answer(value: str | None):
    normalized = _parse_date_answer(value)
    if not normalized:
        return None
    return datetime.strptime(normalized, "%d.%m.%Y").date()



def _parse_time_answer(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip().replace(".", ":")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _time_to_minutes(value: str) -> int:
    hour, minute = [int(x) for x in value.split(":", 1)]
    return hour * 60 + minute


def _minutes_to_time(value: int) -> str:
    value = max(0, min(value, 23 * 60 + 59))
    return f"{value // 60:02d}:{value % 60:02d}"


async def _booking_time_slots() -> list[str]:
    start = _parse_time_answer(await db.get_setting("booking_day_start", "10:00")) or "10:00"
    end = _parse_time_answer(await db.get_setting("booking_day_end", "23:00")) or "23:00"
    try:
        step = int(await db.get_setting("booking_slot_minutes", "60") or 60)
    except ValueError:
        step = 60
    step = max(15, min(step, 240))
    start_min = _time_to_minutes(start)
    end_min = _time_to_minutes(end)
    if end_min <= start_min:
        start_min, end_min = 10 * 60, 23 * 60
    return [_minutes_to_time(value) for value in range(start_min, end_min + 1, step)]


def _selected_date_iso(questions: list[dict], answers: dict[str, str]) -> str | None:
    for question in questions:
        if _question_input_type(question) != "date":
            continue
        parsed = _date_from_answer(answers.get(str(question["id"])))
        if parsed:
            return parsed.isoformat()
    return None


async def _date_fully_busy(date_iso: str) -> bool:
    blocks = await db.list_availability_blocks(date_iso=date_iso, limit=200)
    return any(not block.get("start_time") or not block.get("end_time") for block in blocks)


async def _busy_time_slots(
    date_iso: str | None, slots: list[str], form_id: int | None = None
) -> set[str]:
    if not date_iso:
        return set()
    if form_id is not None:
        try:
            step = int(await db.get_setting("booking_slot_minutes", "60") or 60)
        except ValueError:
            step = 60
        step = max(15, min(step, 240))
        start_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
        pricing = await db.get_form_pricing(form_id)
        before = int(pricing.get("buffer_before_minutes") or 0)
        after = int(pricing.get("buffer_after_minutes") or 0)
        busy: set[str] = set()
        for slot in slots:
            start_minutes = _time_to_minutes(slot)
            end_total = start_minutes + step
            overnight = end_total >= 24 * 60
            end_clock = end_total % (24 * 60)
            end_time = f"{end_clock // 60:02d}:{end_clock % 60:02d}"
            end_date = start_date + timedelta(days=1 if overnight else 0)
            segments = (
                [(date_iso, slot, "24:00"), (end_date.isoformat(), "00:00", end_time)]
                if overnight
                else [(date_iso, slot, end_time)]
            )
            interval = {
                "date_iso": date_iso,
                "start_time": slot,
                "end_time": end_time,
                "end_date_iso": end_date.isoformat(),
                "overnight": overnight,
                "duration_minutes": step,
                "segments": segments,
            }
            if await _booking_interval_conflicts(
                interval, buffer_before_minutes=before, buffer_after_minutes=after
            ):
                busy.add(slot)
        return busy

    blocks = await db.list_availability_blocks(date_iso=date_iso, limit=200)
    busy: set[str] = set()
    for slot in slots:
        point = _time_to_minutes(slot)
        for block in blocks:
            if not block.get("start_time") or not block.get("end_time"):
                busy.add(slot)
                break
            start = _time_to_minutes(str(block["start_time"]))
            end = _time_to_minutes(str(block["end_time"]))
            if start <= point < end:
                busy.add(slot)
                break
    return busy


def _is_end_time_question(question: dict) -> bool:
    label = str(question.get("label") or "").strip().casefold()
    prompt = str(question.get("prompt") or "").strip().casefold()
    markers = ("оконч", "до сколь", "конец", "заверш")
    return _question_input_type(question) == "time" and any(
        marker in label or marker in prompt for marker in markers
    )


def _find_start_time(questions: list[dict], answers: dict[str, str]) -> str | None:
    time_values: list[str] = []
    has_explicit_end = any(_is_end_time_question(q) for q in questions)
    for question in questions:
        if _question_input_type(question) != "time":
            continue
        parsed = _parse_time_answer(answers.get(str(question["id"])))
        if not parsed:
            continue
        time_values.append(parsed)
        if not _is_end_time_question(question):
            return parsed
    if not has_explicit_end and time_values:
        return time_values[0]
    return None


def _find_end_time(questions: list[dict], answers: dict[str, str]) -> str | None:
    time_values: list[str] = []
    for question in questions:
        if _question_input_type(question) != "time":
            continue
        parsed = _parse_time_answer(answers.get(str(question["id"])))
        if not parsed:
            continue
        time_values.append(parsed)
        if _is_end_time_question(question):
            return parsed
    return time_values[1] if len(time_values) >= 2 else None


async def _max_booking_duration_minutes() -> int:
    try:
        hours = int(await db.get_setting("booking_max_duration_hours", "18") or 18)
    except ValueError:
        hours = 18
    return max(1, min(hours, 23)) * 60


def _booking_interval(
    questions: list[dict], answers: dict[str, str]
) -> dict | None:
    """Return booking metadata and one/two same-day availability segments.

    If the end clock is earlier than the start clock, the event is considered to
    finish on the following day. Example: 21:00 -> 05:00 becomes two segments:
    21:00-24:00 on day one and 00:00-05:00 on day two.
    """
    date_iso = _selected_date_iso(questions, answers)
    if not date_iso:
        return None

    start_time = _find_start_time(questions, answers)
    end_time = _find_end_time(questions, answers)
    duration_minutes: int | None = None

    # Backward compatibility for old submissions that still contain duration.
    if not end_time:
        for question in questions:
            label = str(question.get("label") or "").casefold()
            if "продолж" not in label and "длитель" not in label:
                continue
            value = answers.get(str(question["id"]))
            if not value:
                continue
            match = re.search(r"(\d+(?:[.,]\d+)?)", value)
            if match:
                try:
                    duration_minutes = max(15, int(float(match.group(1).replace(",", ".")) * 60))
                except ValueError:
                    pass
            break

    if not start_time:
        return {
            "date_iso": date_iso,
            "start_time": None,
            "end_time": None,
            "end_date_iso": date_iso,
            "overnight": False,
            "duration_minutes": None,
            "segments": [(date_iso, None, None)],
        }

    start_minutes = _time_to_minutes(start_time)
    start_date = datetime.strptime(date_iso, "%Y-%m-%d").date()

    if end_time:
        end_minutes_clock = _time_to_minutes(end_time)
        overnight = end_minutes_clock <= start_minutes
        end_total = end_minutes_clock + (24 * 60 if overnight else 0)
        duration_minutes = end_total - start_minutes
    else:
        if duration_minutes is None:
            duration_minutes = 60
        end_total = start_minutes + duration_minutes
        overnight = end_total >= 24 * 60
        end_minutes_clock = end_total % (24 * 60)
        end_time = f"{end_minutes_clock // 60:02d}:{end_minutes_clock % 60:02d}"

    end_date = start_date + timedelta(days=1 if overnight else 0)
    if overnight:
        segments = [
            (date_iso, start_time, "24:00"),
            (end_date.isoformat(), "00:00", end_time),
        ]
    else:
        segments = [(date_iso, start_time, end_time)]

    return {
        "date_iso": date_iso,
        "start_time": start_time,
        "end_time": end_time,
        "end_date_iso": end_date.isoformat(),
        "overnight": overnight,
        "duration_minutes": duration_minutes,
        "segments": segments,
    }


def _segments_for_datetime_range(start_dt: datetime, end_dt: datetime) -> list[tuple[str, str, str]]:
    """Split a local naive datetime range into per-calendar-day availability segments."""
    if end_dt <= start_dt:
        return []
    segments: list[tuple[str, str, str]] = []
    current_date = start_dt.date()
    final_date = end_dt.date()
    end_exact_midnight = end_dt.time().hour == 0 and end_dt.time().minute == 0 and end_dt.time().second == 0
    if end_exact_midnight:
        final_date = final_date - timedelta(days=1)

    while current_date <= final_date:
        start_time = start_dt.strftime("%H:%M") if current_date == start_dt.date() else "00:00"
        if current_date == final_date:
            if end_exact_midnight:
                end_time = "24:00"
            else:
                end_time = end_dt.strftime("%H:%M")
        else:
            end_time = "24:00"
        if start_time != end_time:
            segments.append((current_date.isoformat(), start_time, end_time))
        current_date += timedelta(days=1)
    return segments


def _booking_interval_with_buffers(
    interval: dict | None, before_minutes: int = 0, after_minutes: int = 0
) -> dict | None:
    if not interval or not interval.get("start_time") or not interval.get("end_time"):
        return interval
    start_date = datetime.strptime(str(interval["date_iso"]), "%Y-%m-%d").date()
    end_date = datetime.strptime(str(interval.get("end_date_iso") or interval["date_iso"]), "%Y-%m-%d").date()
    start_hour, start_minute = [int(x) for x in str(interval["start_time"]).split(":", 1)]
    end_hour, end_minute = [int(x) for x in str(interval["end_time"]).split(":", 1)]
    start_dt = datetime.combine(start_date, datetime.min.time()).replace(hour=start_hour, minute=start_minute)
    end_dt = datetime.combine(end_date, datetime.min.time()).replace(hour=end_hour, minute=end_minute)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    before = max(0, min(int(before_minutes or 0), 24 * 60))
    after = max(0, min(int(after_minutes or 0), 24 * 60))
    tech_start = start_dt - timedelta(minutes=before)
    tech_end = end_dt + timedelta(minutes=after)
    result = dict(interval)
    result.update({
        "buffer_before_minutes": before,
        "buffer_after_minutes": after,
        "technical_start": tech_start,
        "technical_end": tech_end,
        "technical_segments": _segments_for_datetime_range(tech_start, tech_end),
    })
    return result


async def _booking_interval_conflicts(
    interval: dict | None, *, exclude_submission_id: int | None = None,
    buffer_before_minutes: int = 0, buffer_after_minutes: int = 0
) -> bool:
    if not interval:
        return False
    buffered = _booking_interval_with_buffers(interval, buffer_before_minutes, buffer_after_minutes)
    segments = (buffered or {}).get("technical_segments") or interval.get("segments") or []
    for date_iso, start_time, end_time in segments:
        if await db.booking_conflicts(
            date_iso, start_time, end_time, exclude_submission_id=exclude_submission_id
        ):
            return True
    return False


async def _booking_interval_conflicts_for_form(
    form_id: int, interval: dict | None, *, exclude_submission_id: int | None = None
) -> bool:
    pricing = await db.get_form_pricing(form_id)
    return await _booking_interval_conflicts(
        interval,
        exclude_submission_id=exclude_submission_id,
        buffer_before_minutes=int(pricing.get("buffer_before_minutes") or 0),
        buffer_after_minutes=int(pricing.get("buffer_after_minutes") or 0),
    )


async def _booking_end_time_options(
    form_id: int, questions: list[dict], answers: dict[str, str]
) -> tuple[list[tuple[str, str]], set[str]]:
    start_time = _find_start_time(questions, answers)
    if not start_time:
        slots = await _booking_time_slots()
        return [(slot, slot) for slot in slots], set()

    try:
        step = int(await db.get_setting("booking_slot_minutes", "60") or 60)
    except ValueError:
        step = 60
    step = max(15, min(step, 240))
    max_duration = await _max_booking_duration_minutes()
    start_minutes = _time_to_minutes(start_time)
    pricing = await db.get_form_pricing(form_id)
    before = int(pricing.get("buffer_before_minutes") or 0)
    after = int(pricing.get("buffer_after_minutes") or 0)

    options: list[tuple[str, str]] = []
    busy: set[str] = set()
    end_question = next((q for q in questions if _is_end_time_question(q)), None)
    for offset in range(step, max_duration + 1, step):
        total = start_minutes + offset
        value_minutes = total % (24 * 60)
        value = f"{value_minutes // 60:02d}:{value_minutes % 60:02d}"
        label = f"{value} +1д" if total >= 24 * 60 else value
        options.append((value, label))

        if end_question:
            temp_answers = dict(answers)
            temp_answers[str(end_question["id"])] = value
            if await _booking_interval_conflicts(
                _booking_interval(questions, temp_answers),
                buffer_before_minutes=before, buffer_after_minutes=after,
            ):
                busy.add(value)
    return options, busy


def _booking_interval_summary(interval: dict | None) -> str | None:
    if not interval or not interval.get("start_time") or not interval.get("end_time"):
        return None
    duration = int(interval.get("duration_minutes") or 0)
    hours, minutes = divmod(duration, 60)
    duration_text = f"{hours} ч" if not minutes else f"{hours} ч {minutes} мин"
    next_day = " (+1 день)" if interval.get("overnight") else ""
    return f"{interval['start_time']}–{interval['end_time']}{next_day} · {duration_text}"


def _duration_minutes_text(value: int | str | None) -> str:
    try:
        minutes = max(0, int(value or 0))
    except (TypeError, ValueError):
        minutes = 0
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    if hours:
        return f"{hours} ч"
    return f"{rest} мин" if rest else "0 мин"


def _parse_buffer_minutes(value: str | None) -> int | None:
    if value is None:
        return None
    raw = value.strip().casefold().replace(" ", "")
    if not raw:
        return None
    if raw.isdigit():
        number = int(raw)
        return number if 0 <= number <= 1440 else None
    match = re.fullmatch(r"(\d{1,2})(?:ч|h)(?:(\d{1,2})(?:м|мин|min)?)?", raw)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2) or 0)
        total = hours * 60 + minutes
        return total if 0 <= total <= 1440 and minutes < 60 else None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if match:
        hours, minutes = int(match.group(1)), int(match.group(2))
        total = hours * 60 + minutes
        return total if 0 <= total <= 1440 and minutes < 60 else None
    return None


async def _selected_addon_rows(form_id: int, selected_ids: list[int] | None) -> list[dict]:
    return await db.get_selected_addons(form_id, selected_ids or [])


def _addons_text(addons: list[dict], currency: str, *, with_total: bool = True) -> str | None:
    if not addons:
        return None
    lines = ["🧰 Дополнительные услуги:"]
    total = 0
    for addon in addons:
        amount = max(0, int(addon.get("amount") or 0))
        total += amount
        lines.append(f"• {addon.get('name')}: {_money_text(amount, currency)}")
    if with_total:
        lines.append(f"Доп. услуги всего: {_money_text(total, currency)}")
    return "\n".join(lines)


SUBMISSION_STATUS_NAMES = {
    "new": "🆕 Новая",
    "in_progress": "🟡 В работе",
    "confirmed": "✅ Подтверждена",
    "paid": "💰 Оплачена",
    "completed": "🏁 Завершена",
    "cancelled": "❌ Отказ",
}
BOOKING_STATUSES = {"confirmed", "paid", "completed"}


def _money_text(value: int | str | None, currency: str = "₽") -> str:
    try:
        number = max(0, int(value or 0))
    except (TypeError, ValueError):
        number = 0
    return f"{number:,}".replace(",", " ") + f" {currency}" if number else "—"


def _parse_money_input(value: str | None) -> int | None:
    if not value:
        return None
    raw = value.strip().replace(" ", "").replace(" ", "")
    raw = re.sub(r"[^0-9]", "", raw)
    if not raw:
        return None
    try:
        number = int(raw)
    except ValueError:
        return None
    if number < 0 or number > 1_000_000_000:
        return None
    return number


async def _calculate_form_pricing(
    form_id: int, questions: list[dict], answers: dict[str, str],
    selected_addon_ids: list[int] | None = None,
) -> dict | None:
    """Calculate a preliminary rental price for a time-based form.

    The base amount covers `included_hours`. Every started hour beyond that
    is billed at `extra_hour_amount`. With included_hours=0 the extra-hour
    rate acts as a straight hourly tariff; base_amount may still be used as
    a fixed starting fee.
    """
    pricing = await db.get_form_pricing(form_id)
    if not pricing.get("enabled"):
        return None
    interval = _booking_interval(questions, answers)
    if not interval or not interval.get("start_time") or not interval.get("end_time"):
        return None
    duration_minutes = int(interval.get("duration_minutes") or 0)
    if duration_minutes <= 0:
        return None

    base_amount = max(0, int(pricing.get("base_amount") or 0))
    included_hours = max(0, int(pricing.get("included_hours") or 0))
    extra_hour_amount = max(0, int(pricing.get("extra_hour_amount") or 0))
    included_minutes = included_hours * 60

    if included_hours > 0:
        extra_minutes = max(0, duration_minutes - included_minutes)
        extra_hours = (extra_minutes + 59) // 60 if extra_minutes else 0
        billed_hours = included_hours + extra_hours
        amount = base_amount + extra_hours * extra_hour_amount
    else:
        billed_hours = (duration_minutes + 59) // 60
        extra_hours = billed_hours
        amount = base_amount + billed_hours * extra_hour_amount

    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    selected_addons = await _selected_addon_rows(form_id, selected_addon_ids)
    addon_items = [
        {"id": int(item["id"]), "name": str(item["name"]), "amount": max(0, int(item.get("amount") or 0))}
        for item in selected_addons
    ]
    addons_total = sum(int(item["amount"]) for item in addon_items)
    rental_amount = amount
    amount = rental_amount + addons_total
    return {
        "amount": amount,
        "rental_amount": rental_amount,
        "addons_total": addons_total,
        "addons": addon_items,
        "currency": currency,
        "duration_minutes": duration_minutes,
        "base_amount": base_amount,
        "included_hours": included_hours,
        "extra_hour_amount": extra_hour_amount,
        "extra_hours": extra_hours,
        "billed_hours": billed_hours,
        "start_time": interval.get("start_time"),
        "end_time": interval.get("end_time"),
        "overnight": bool(interval.get("overnight")),
        "buffer_before_minutes": max(0, int(pricing.get("buffer_before_minutes") or 0)),
        "buffer_after_minutes": max(0, int(pricing.get("buffer_after_minutes") or 0)),
    }


def _pricing_calculation_text(calculation: dict | None) -> str | None:
    if not calculation:
        return None
    currency = str(calculation.get("currency") or "₽")
    amount = int(calculation.get("amount") or 0)
    rental_amount = int(calculation.get("rental_amount") or amount)
    addons = list(calculation.get("addons") or [])
    addons_total = int(calculation.get("addons_total") or 0)
    base = int(calculation.get("base_amount") or 0)
    included = int(calculation.get("included_hours") or 0)
    extra_rate = int(calculation.get("extra_hour_amount") or 0)
    extra_hours = int(calculation.get("extra_hours") or 0)
    duration = int(calculation.get("duration_minutes") or 0)
    hours, minutes = divmod(duration, 60)
    duration_text = f"{hours} ч" if not minutes else f"{hours} ч {minutes} мин"

    lines = [f"💰 Предварительная стоимость: {_money_text(amount, currency)}"]
    if addons:
        lines.append(f"Аренда: {_money_text(rental_amount, currency)}")
        for addon in addons:
            lines.append(f"+ {addon.get('name')}: {_money_text(addon.get('amount'), currency)}")
        lines.append(f"Доп. услуги: {_money_text(addons_total, currency)}")
    if included > 0:
        tariff = f"{_money_text(base, currency)} за первые {included} ч"
        if extra_rate:
            tariff += f" + {_money_text(extra_rate, currency)} за каждый начатый дополнительный час"
        lines.append(f"Тариф: {tariff}.")
        if extra_hours:
            lines.append(f"Дополнительное время к расчёту: {extra_hours} ч.")
    elif extra_rate:
        tariff = f"{_money_text(extra_rate, currency)} за каждый начатый час"
        if base:
            tariff = f"база {_money_text(base, currency)} + {tariff}"
        lines.append(f"Тариф: {tariff}.")
    elif base:
        lines.append(f"Фиксированная стоимость: {_money_text(base, currency)}.")
    lines.append(f"Продолжительность: {duration_text}.")
    lines.append("Итоговая стоимость может быть скорректирована администратором.")
    return "\n".join(lines)


def _form_supports_time_pricing(questions: list[dict]) -> bool:
    has_date = any(_question_input_type(q) == "date" for q in questions)
    time_questions = [q for q in questions if _question_input_type(q) == "time"]
    has_end = any(_is_end_time_question(q) for q in time_questions)
    has_duration = any(
        "продолж" in str(q.get("label") or "").casefold()
        or "длитель" in str(q.get("label") or "").casefold()
        for q in questions
    )
    return has_date and bool(time_questions) and (has_end or has_duration or len(time_questions) >= 2)


async def _pricing_admin_text(form: dict, pricing: dict) -> str:
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    enabled = bool(pricing.get("enabled"))
    base = max(0, int(pricing.get("base_amount") or 0))
    included = max(0, int(pricing.get("included_hours") or 0))
    extra = max(0, int(pricing.get("extra_hour_amount") or 0))
    buffer_before = max(0, int(pricing.get("buffer_before_minutes") or 0))
    buffer_after = max(0, int(pricing.get("buffer_after_minutes") or 0))
    addons = await db.list_form_addons(int(form["id"]))
    enabled_addons = [item for item in addons if item.get("enabled")]
    questions = await db.list_form_questions(int(form["id"]))
    compatible = _form_supports_time_pricing(questions)
    lines = [
        f"<b>💰 Тариф · {html.escape(str(form['name']))}</b>",
        "",
        f"Расчёт: {'🟢 включён' if enabled else '⚪ выключен'}",
        f"Базовая стоимость: <b>{html.escape(_money_text(base, currency))}</b>",
        f"В базовую стоимость включено: <b>{included} ч</b>",
        f"Каждый начатый дополнительный час: <b>{html.escape(_money_text(extra, currency))}</b>",
        f"Технический буфер до: <b>{html.escape(_duration_minutes_text(buffer_before))}</b>",
        f"Технический буфер после: <b>{html.escape(_duration_minutes_text(buffer_after))}</b>",
        f"Дополнительных услуг: <b>{len(enabled_addons)} активных / {len(addons)} всего</b>",
        "",
    ]
    if included > 0:
        lines.append(
            f"Формула: {_money_text(base, currency)} за первые {included} ч"
            + (f" + {_money_text(extra, currency)} за каждый начатый доп. час." if extra else ".")
        )
    elif extra > 0:
        prefix = f"{_money_text(base, currency)} + " if base else ""
        lines.append(f"Формула: {prefix}{_money_text(extra, currency)} за каждый начатый час.")
    elif base > 0:
        lines.append(f"Формула: фиксированно {_money_text(base, currency)}.")
    else:
        lines.append("Тариф ещё не настроен. Укажите базовую стоимость и/или цену часа.")
    lines.extend(
        [
            "",
            (
                "✅ В форме есть дата, начало и окончание — автоматический расчёт доступен."
                if compatible
                else "⚠️ Для расчёта по времени форме нужны дата, время начала и время окончания."
            ),
            "",
            "Цена показывается клиенту как предварительная и автоматически записывается в стоимость новой заявки. "
            "Выбранные дополнительные услуги прибавляются к расчёту. Технический буфер влияет только на занятость календаря, "
            "но не увеличивает оплачиваемую длительность. После этого администратор может изменить стоимость вручную.",
        ]
    )
    return "\n".join(lines)


async def _submission_status_notification_text(submission: dict, new_status: str) -> str:
    """Build a client-facing status update from an editable admin template."""
    status_text = SUBMISSION_STATUS_NAMES.get(new_status, new_status)
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    amount = max(0, int(submission.get("total_amount") or 0))
    prepayment = max(0, int(submission.get("prepayment_amount") or 0))
    balance = max(0, amount - prepayment) if amount else 0
    template = await db.get_setting(
        f"status_template_{new_status}", DEFAULT_STATUS_TEMPLATES.get(new_status, "{status}")
    )
    template = template or DEFAULT_STATUS_TEMPLATES.get(new_status, "{status}")
    full_name = " ".join(
        part for part in [submission.get("first_name"), submission.get("last_name")] if part
    ).strip() or "клиент"
    values = {
        "{id}": str(submission.get("id") or ""),
        "{form}": str(submission.get("form_name") or "Заявка"),
        "{status}": status_text,
        "{client}": full_name,
        "{amount}": _money_text(amount, currency),
        "{prepayment}": _money_text(prepayment, currency),
        "{balance}": _money_text(balance, currency),
    }
    result = str(template)
    for token, value in values.items():
        result = result.replace(token, value)
    return result[:4000]


async def _notify_submission_status_change(
    bot: Bot, submission: dict, new_status: str
) -> tuple[bool, str | None]:
    """Notify the customer in the original Telegram Business dialog.

    Returns (sent, error_reason). Old submissions created before v1.4 may not
    contain business_connection_id, so for a single-account installation we
    fall back to the latest active Business connection.
    """
    chat_id = submission.get("chat_id")
    if not chat_id:
        return False, "у заявки нет chat_id"

    business_connection_id = submission.get("business_connection_id")
    if not business_connection_id:
        connection = await db.latest_business_connection()
        if connection and connection.get("enabled") and connection.get("can_reply"):
            business_connection_id = connection.get("id")

    if not business_connection_id:
        return False, "не найдено Business-соединение"

    try:
        await bot.send_message(
            chat_id=int(chat_id),
            business_connection_id=str(business_connection_id),
            text=await _submission_status_notification_text(submission, new_status),
            parse_mode=None,
        )
        return True, None
    except TelegramAPIError as exc:
        logger.exception(
            "Не удалось уведомить клиента о статусе заявки %s -> %s",
            submission.get("id"),
            new_status,
        )
        return False, str(exc)


async def _notify_submission_amount_change(
    bot: Bot, submission: dict, old_amount: int, new_amount: int
) -> tuple[bool, str | None]:
    """Notify the customer when an administrator manually changes the request total."""
    chat_id = submission.get("chat_id")
    if not chat_id:
        return False, "у заявки нет chat_id"

    business_connection_id = submission.get("business_connection_id")
    if not business_connection_id:
        connection = await db.latest_business_connection()
        if connection and connection.get("enabled") and connection.get("can_reply"):
            business_connection_id = connection.get("id")
    if not business_connection_id:
        return False, "не найдено Business-соединение"

    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    prepayment = max(0, int(submission.get("prepayment_amount") or 0))
    balance = max(0, new_amount - prepayment) if new_amount else 0
    text = (
        f"💵 Стоимость заявки №{submission.get('id')} изменена\n\n"
        f"{submission.get('form_name') or 'Заявка'}\n"
        f"Было: {_money_text(old_amount, currency)}\n"
        f"Стало: {_money_text(new_amount, currency)}\n"
        f"Предоплата: {_money_text(prepayment, currency)}\n"
        f"Остаток: {_money_text(balance, currency)}\n\n"
        "Если есть вопросы по стоимости, ответьте в этом чате."
    )
    try:
        await bot.send_message(
            chat_id=int(chat_id),
            business_connection_id=str(business_connection_id),
            text=text,
            parse_mode=None,
        )
        return True, None
    except TelegramAPIError as exc:
        logger.exception(
            "Не удалось уведомить клиента об изменении стоимости заявки %s",
            submission.get("id"),
        )
        return False, str(exc)


async def _submission_admin_text(submission: dict) -> str:
    full_name = " ".join(
        part for part in [submission.get("first_name"), submission.get("last_name")] if part
    ).strip() or "Без имени"
    status = SUBMISSION_STATUS_NAMES.get(str(submission.get("status") or "new"), "•")
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    total_amount = max(0, int(submission.get("total_amount") or 0))
    prepayment = max(0, int(submission.get("prepayment_amount") or 0))
    balance = max(0, total_amount - prepayment) if total_amount else 0
    pricing_details = submission.get("pricing_details") or {}
    addon_items = list(pricing_details.get("addons") or [])
    if not addon_items and submission.get("form_id") and submission.get("selected_addon_ids"):
        addon_items = await _selected_addon_rows(
            int(submission["form_id"]), submission.get("selected_addon_ids") or []
        )
    lines = [
        f"<b>Заявка №{submission['id']}</b>",
        f"Статус: {status}",
        f"Форма: <b>{html.escape(str(submission['form_name']))}</b>",
        "",
        f"💵 Стоимость: <b>{html.escape(_money_text(total_amount, currency))}</b>",
        (
            f"🤖 Расчёт тарифа: <b>{html.escape(_money_text(submission.get('calculated_amount'), currency))}</b>"
            + (" · изменено вручную" if int(submission.get("calculated_amount") or 0) and int(submission.get("calculated_amount") or 0) != total_amount else "")
        ),
        f"💳 Предоплата: <b>{html.escape(_money_text(prepayment, currency))}</b>",
        f"🧾 Остаток: <b>{html.escape(_money_text(balance, currency))}</b>",
        f"🗒 Заметка: {html.escape(str(submission.get('internal_note') or '—'))}",
    ]
    if addon_items:
        lines.extend(["", "<b>🧰 Дополнительные услуги:</b>"])
        for addon in addon_items:
            lines.append(
                f"• {html.escape(str(addon.get('name') or 'Услуга'))}: "
                f"{html.escape(_money_text(addon.get('amount'), currency))}"
            )
    lines.extend([
        "",
        f"Клиент: {html.escape(full_name)}",
        f"Telegram: @{html.escape(str(submission['username']))}" if submission.get("username") else "Telegram: —",
        f"User ID: {submission.get('user_id') or '—'}",
        "",
    ])
    questions = await db.list_form_questions(int(submission["form_id"])) if submission.get("form_id") else []
    answers = submission.get("answers") or {}
    if questions:
        for question in questions:
            value = answers.get(str(question["id"])) or "—"
            lines.append(f"<b>{html.escape(str(question['label']))}:</b> {html.escape(str(value))}")
        interval = _booking_interval(questions, answers)
        interval_summary = _booking_interval_summary(interval)
        if interval_summary:
            lines.extend(["", f"<b>🕐 Интервал:</b> {html.escape(interval_summary)}"])
            pricing = await db.get_form_pricing(int(submission["form_id"]))
            before = int(pricing.get("buffer_before_minutes") or 0)
            after = int(pricing.get("buffer_after_minutes") or 0)
            if before or after:
                buffered = _booking_interval_with_buffers(interval, before, after)
                if buffered and buffered.get("technical_start") and buffered.get("technical_end"):
                    tech_start = buffered["technical_start"].strftime("%d.%m %H:%M")
                    tech_end = buffered["technical_end"].strftime("%d.%m %H:%M")
                    lines.append(
                        f"<b>🔧 Тех. занятость:</b> {html.escape(tech_start)} → {html.escape(tech_end)} "
                        f"(до {_duration_minutes_text(before)}, после {_duration_minutes_text(after)})"
                    )
    else:
        for key, value in answers.items():
            lines.append(f"<b>Поле {html.escape(str(key))}:</b> {html.escape(str(value))}")
    lines.extend(["", f"Создана: {html.escape(_format_local_timestamp(submission.get('created_at')))} ({html.escape(settings.timezone_name)})"])
    return "\n".join(lines)


def _normalize_phone_answer(value: str | None) -> str | None:
    """Validate and normalize a manually entered phone number.

    We intentionally do not assume a country. Accept common separators, require
    7-15 digits (E.164 maximum), preserve a leading + and convert 00-prefix to +.
    """
    if not value:
        return None
    raw = value.strip()
    if not raw or len(raw) > 40:
        return None
    # Only phone-like punctuation is accepted; letters and arbitrary text are rejected.
    if not re.fullmatch(r"\+?[0-9][0-9\s().\-]*", raw) and not re.fullmatch(
        r"00[0-9][0-9\s().\-]*", raw
    ):
        return None
    digits = re.sub(r"\D", "", raw)
    if not 7 <= len(digits) <= 15:
        return None
    if raw.startswith("00"):
        international = digits[2:]
        if not 7 <= len(international) <= 15:
            return None
        return "+" + international
    if raw.startswith("+"):
        return "+" + digits
    return digits


def _contact_question_text(
    form: dict,
    question: dict,
    *,
    index: int,
    total: int,
    suffix: str = "",
    error: str | None = None,
) -> str:
    lines = [
        f"📝 {form['name']}",
        "",
        f"Вопрос {index + 1} из {total}",
        f"{question['prompt']}{suffix}",
        "",
    ]
    if error:
        lines.extend([f"⚠️ {error}", ""])
    lines.extend(
        [
            "Введите номер вручную, например:",
            "+7 999 123-45-67",
            "",
            "Допустимо от 7 до 15 цифр. Если Telegram позволяет отправить контакт через вложение, можно прислать свой контакт — бот тоже его примет.",
        ]
    )
    return "\n".join(lines)


def _message_answer_text(message: Message) -> str | None:
    if message.text and message.text.strip():
        return message.text.strip()
    if message.contact and message.contact.phone_number:
        return message.contact.phone_number.strip()
    if message.location:
        return f"{message.location.latitude:.6f}, {message.location.longitude:.6f}"
    return None


def _form_preview_text(
    form: dict, questions: list[dict], answers: dict[str, str],
    pricing_calculation: dict | None = None, selected_addons: list[dict] | None = None,
    currency: str = "₽"
) -> str:
    lines = ["✅ Проверьте заявку", "", str(form["name"]), ""]
    for question in questions:
        answer = answers.get(str(question["id"])) or "—"
        lines.append(f"{question['label']}: {answer}")
    interval_summary = _booking_interval_summary(_booking_interval(questions, answers))
    if interval_summary:
        lines.extend(["", f"🕐 Интервал: {interval_summary}"])
    if selected_addons and not pricing_calculation:
        addon_text = _addons_text(selected_addons, currency)
        if addon_text:
            lines.extend(["", addon_text])
    pricing_text = _pricing_calculation_text(pricing_calculation)
    if pricing_text:
        lines.extend(["", pricing_text])
    lines.append("")
    lines.append("Если всё верно, нажмите «Отправить заявку».")
    return "\n".join(lines)


async def _upsert_form_message(
    bot: Bot, session: dict, *, text: str, reply_markup=None
) -> int | None:
    """Keep the whole questionnaire in one editable Business message."""
    message_id = session.get("form_message_id")
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=session["chat_id"],
                message_id=int(message_id),
                business_connection_id=session["business_connection_id"],
                text=text,
                parse_mode=None,
                reply_markup=reply_markup,
            )
            return int(message_id)
        except TelegramAPIError:
            logger.warning(
                "Не удалось обновить сообщение формы %s, отправляю новое", message_id
            )

    try:
        sent = await bot.send_message(
            chat_id=session["chat_id"],
            business_connection_id=session["business_connection_id"],
            text=text,
            parse_mode=None,
            reply_markup=reply_markup,
        )
    except TelegramAPIError:
        logger.exception("Не удалось отправить сообщение формы в chat_id=%s", session["chat_id"])
        return None

    await db.update_form_session(
        int(session["chat_id"]), form_message_id=int(sent.message_id)
    )
    session["form_message_id"] = int(sent.message_id)
    return int(sent.message_id)


async def _delete_form_answer_message(
    bot: Bot, message: Message, session: dict, can_delete_all_messages: bool
) -> None:
    if not can_delete_all_messages:
        return
    try:
        await bot.delete_business_messages(
            business_connection_id=session["business_connection_id"],
            message_ids=[message.message_id],
        )
    except TelegramAPIError:
        logger.warning(
            "Не удалось удалить ответ формы message_id=%s. Проверьте право «Удаление входящих».",
            message.message_id,
        )


async def _form_callback_session(callback: CallbackQuery) -> dict | None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return None
    session = await db.get_form_session(callback.message.chat.id)
    if not session:
        await callback.answer("Заявка уже закрыта", show_alert=True)
        return None
    active_message_id = session.get("form_message_id")
    if active_message_id and int(active_message_id) != int(callback.message.message_id):
        await callback.answer(
            "Эта кнопка относится к старому шагу формы и больше не активна.",
            show_alert=True,
        )
        return None
    return session


async def send_current_form_question(
    bot: Bot, chat_id: int, *, calendar_year: int | None = None, calendar_month: int | None = None
) -> None:
    session = await db.get_form_session(chat_id)
    if not session:
        return
    form = await db.get_form(int(session["form_id"]))
    questions = await db.list_form_questions(int(session["form_id"]))
    if not form or not questions:
        await _upsert_form_message(
            bot,
            session,
            text="Эта форма пока не настроена. Напишите сообщение обычным текстом.",
            reply_markup=None,
        )
        await db.delete_form_session(chat_id)
        return

    index = max(0, int(session["current_index"]))
    if index >= len(questions):
        addons = await db.list_form_addons(int(form["id"]), enabled_only=True)
        if addons and not bool(session.get("addons_confirmed")):
            selected_ids = set(int(x) for x in session.get("selected_addon_ids") or [])
            currency = str(await db.get_setting("crm_currency", "₽") or "₽")
            selected_rows = [item for item in addons if int(item["id"]) in selected_ids]
            addon_total = sum(max(0, int(item.get("amount") or 0)) for item in selected_rows)
            lines = [
                f"🧰 {form['name']}",
                "",
                "Выберите дополнительные услуги. Можно выбрать несколько вариантов.",
            ]
            if selected_rows:
                lines.extend(["", "Выбрано:"])
                for item in selected_rows:
                    lines.append(f"• {item['name']} — {_money_text(item.get('amount'), currency)}")
                lines.append(f"Всего доп. услуг: {_money_text(addon_total, currency)}")
            else:
                lines.extend(["", "Пока ничего не выбрано."])
            live_calculation = await _calculate_form_pricing(
                int(form["id"]), questions, session["answers"], list(selected_ids)
            )
            if live_calculation:
                lines.extend([
                    "",
                    f"💰 Предварительный итог с услугами: {_money_text(live_calculation.get('amount'), currency)}",
                ])
            await db.update_form_session(
                chat_id, current_index=len(questions), status="addons", keyboard_question_id=0
            )
            session = await db.get_form_session(chat_id)
            if not session:
                return
            await _upsert_form_message(
                bot, session, text="\n".join(lines),
                reply_markup=form_addons(addons, selected_ids, currency),
            )
            return

        await db.update_form_session(
            chat_id, current_index=len(questions), status="confirm", keyboard_question_id=0
        )
        session = await db.get_form_session(chat_id)
        if not session:
            return
        selected_rows = await _selected_addon_rows(
            int(form["id"]), session.get("selected_addon_ids") or []
        )
        pricing_calculation = await _calculate_form_pricing(
            int(form["id"]), questions, session["answers"], session.get("selected_addon_ids") or []
        )
        currency = str(await db.get_setting("crm_currency", "₽") or "₽")
        await _upsert_form_message(
            bot,
            session,
            text=_form_preview_text(
                form, questions, session["answers"], pricing_calculation, selected_rows, currency
            ),
            reply_markup=form_confirmation(has_addons=bool(addons)),
        )
        return

    question = questions[index]
    input_type = _question_input_type(question)
    suffix = "\n\nЭтот вопрос необязательный — его можно пропустить." if not question["required"] else ""
    existing = session["answers"].get(str(question["id"]))
    if existing:
        suffix += f"\n\nТекущий ответ: {existing}"

    if input_type == "date":
        today = local_today()
        selected = _date_from_answer(existing)
        year = calendar_year or (selected.year if selected else today.year)
        month = calendar_month or (selected.month if selected else today.month)
        if year < today.year - 1:
            year, month = today.year, today.month
        if year > today.year + 6:
            year, month = today.year + 6, 12
        text = (
            f"📝 {form['name']}\n\n"
            f"Вопрос {index + 1} из {len(questions)}\n"
            f"{question['prompt']}{suffix}\n\n"
            "Выберите день в календаре или введите дату вручную в формате ДД.ММ.ГГГГ.\n"
            "× — дата занята полностью, • — есть занятые часы."
        )
        full_busy_dates, partial_busy_dates = await db.month_availability(year, month)
        markup = calendar_keyboard(
            int(question["id"]),
            year,
            month,
            calendar.monthcalendar(year, month),
            required=bool(question["required"]),
            can_go_back=index > 0,
            today_iso=today.isoformat(),
            full_busy_dates=full_busy_dates,
            partial_busy_dates=partial_busy_dates,
        )
        await _upsert_form_message(bot, session, text=text, reply_markup=markup)
        return

    if input_type == "time":
        date_iso = _selected_date_iso(questions, session["answers"])
        if _is_end_time_question(question):
            options, busy_values = await _booking_end_time_options(int(form["id"]), questions, session["answers"])
            start_time = _find_start_time(questions, session["answers"])
            text = (
                f"📝 {form['name']}\n\n"
                f"Вопрос {index + 1} из {len(questions)}\n"
                f"{question['prompt']}{suffix}\n\n"
            )
            if start_time:
                text += (
                    f"Начало: {start_time}. Выберите время окончания.\n"
                    "Время после полуночи отмечено «+1д» и относится к следующему дню.\n"
                    "Можно также ввести время вручную в формате ЧЧ:ММ."
                )
            else:
                text += "Сначала укажите время начала или введите окончание вручную в формате ЧЧ:ММ."
            if date_iso:
                text += "\n× — этот интервал пересекается с уже занятой бронью."
            await _upsert_form_message(
                bot,
                session,
                text=text,
                reply_markup=end_time_slots_keyboard(
                    int(question["id"]),
                    options,
                    busy_values,
                    required=bool(question["required"]),
                    can_go_back=index > 0,
                ),
            )
            return

        slots = await _booking_time_slots()
        busy_slots = await _busy_time_slots(date_iso, slots, int(form["id"]))
        text = (
            f"📝 {form['name']}\n\n"
            f"Вопрос {index + 1} из {len(questions)}\n"
            f"{question['prompt']}{suffix}\n\n"
            "Выберите свободное время начала или введите его вручную в формате ЧЧ:ММ."
        )
        if date_iso:
            text += "\n× — время уже занято."
        await _upsert_form_message(
            bot, session, text=text,
            reply_markup=time_slots_keyboard(
                int(question["id"]), slots, busy_slots,
                required=bool(question["required"]), can_go_back=index > 0,
            ),
        )
        return

    if input_type == "guest_count":
        text = (
            f"📝 {form['name']}\n\n"
            f"Вопрос {index + 1} из {len(questions)}\n"
            f"{question['prompt']}{suffix}\n\n"
            "Выберите диапазон количества гостей. Можно также ввести число вручную."
        )
        await _upsert_form_message(
            bot,
            session,
            text=text,
            reply_markup=guest_count_keyboard(
                int(question["id"]),
                required=bool(question["required"]),
                can_go_back=index > 0,
            ),
        )
        return

    if input_type == "contact":
        text = _contact_question_text(
            form,
            question,
            index=index,
            total=len(questions),
            suffix=suffix,
        )
    else:
        text = (
            f"📝 {form['name']}\n\n"
            f"Вопрос {index + 1} из {len(questions)}\n"
            f"{question['prompt']}{suffix}"
        )

    await _upsert_form_message(
        bot,
        session,
        text=text,
        reply_markup=form_question_nav(bool(question["required"]), index > 0),
    )


async def handle_form_message(
    message: Message, bot: Bot, session: dict, *, can_delete_all_messages: bool
) -> bool:
    if session["status"] == "confirm":
        await _delete_form_answer_message(bot, message, session, can_delete_all_messages)
        addons = await db.list_form_addons(int(session["form_id"]), enabled_only=True)
        await _upsert_form_message(
            bot,
            session,
            text="Заявка уже заполнена. Используйте кнопки «Отправить заявку», «Изменить ответы» или «Отмена».",
            reply_markup=form_confirmation(has_addons=bool(addons)),
        )
        return True

    if (message.text or "").strip().lower() in {"/cancel", "отмена", "отменить"}:
        await _delete_form_answer_message(bot, message, session, can_delete_all_messages)
        await _upsert_form_message(bot, session, text="Заявка отменена.", reply_markup=None)
        await db.delete_form_session(message.chat.id)
        return True

    questions = await db.list_form_questions(int(session["form_id"]))
    index = int(session["current_index"])
    if index < 0 or index >= len(questions):
        await send_current_form_question(bot, message.chat.id)
        return True

    question = questions[index]
    input_type = _question_input_type(question)

    if input_type == "contact":
        raw_phone: str | None
        if message.contact:
            if (
                message.contact.user_id
                and message.from_user
                and int(message.contact.user_id) != int(message.from_user.id)
            ):
                await _delete_form_answer_message(
                    bot, message, session, can_delete_all_messages
                )
                form = await db.get_form(int(session["form_id"]))
                if form:
                    await _upsert_form_message(
                        bot,
                        session,
                        text=_contact_question_text(
                            form,
                            question,
                            index=index,
                            total=len(questions),
                            error="Отправлен чужой контакт. Укажите свой номер вручную или пришлите свой контакт.",
                        ),
                        reply_markup=form_question_nav(bool(question["required"]), index > 0),
                    )
                return True
            raw_phone = message.contact.phone_number
        else:
            raw_phone = message.text

        answer = _normalize_phone_answer(raw_phone)
        if not answer:
            await _delete_form_answer_message(
                bot, message, session, can_delete_all_messages
            )
            form = await db.get_form(int(session["form_id"]))
            if form:
                await _upsert_form_message(
                    bot,
                    session,
                    text=_contact_question_text(
                        form,
                        question,
                        index=index,
                        total=len(questions),
                        error=(
                            "Не похоже на номер телефона. Введите 7–15 цифр с кодом страны, "
                            "например +7 999 123-45-67."
                        ),
                    ),
                    reply_markup=form_question_nav(bool(question["required"]), index > 0),
                )
            return True
    elif input_type == "guest_count":
        answer = _normalize_guest_count_answer(message.text)
        if not answer:
            await _delete_form_answer_message(bot, message, session, can_delete_all_messages)
            await send_current_form_question(bot, message.chat.id)
            return True
    elif input_type == "date":
        answer = _parse_date_answer(message.text)
        if not answer:
            await send_current_form_question(bot, message.chat.id)
            return True
        parsed_date = _date_from_answer(answer)
        if parsed_date and await _date_fully_busy(parsed_date.isoformat()):
            await send_current_form_question(bot, message.chat.id)
            return True
    elif input_type == "time":
        answer = _parse_time_answer(message.text)
        if not answer:
            await _delete_form_answer_message(bot, message, session, can_delete_all_messages)
            await send_current_form_question(bot, message.chat.id)
            return True

        temp_answers = dict(session["answers"])
        temp_answers[str(question["id"])] = answer
        interval = _booking_interval(questions, temp_answers)

        if _is_end_time_question(question) and interval and interval.get("start_time"):
            max_duration = await _max_booking_duration_minutes()
            if int(interval.get("duration_minutes") or 0) > max_duration:
                await _delete_form_answer_message(bot, message, session, can_delete_all_messages)
                await _upsert_form_message(
                    bot,
                    session,
                    text=(
                        f"⚠️ Слишком длинный интервал. Максимум — {max_duration // 60} ч.\n\n"
                        "Выберите другое время окончания."
                    ),
                    reply_markup=form_question_nav(bool(question["required"]), index > 0),
                )
                return True
            if await _booking_interval_conflicts_for_form(int(session["form_id"]), interval):
                await _delete_form_answer_message(bot, message, session, can_delete_all_messages)
                await send_current_form_question(bot, message.chat.id)
                return True
        else:
            date_iso = _selected_date_iso(questions, temp_answers)
            existing_end = _find_end_time(questions, temp_answers)
            if existing_end and interval:
                max_duration = await _max_booking_duration_minutes()
                if int(interval.get("duration_minutes") or 0) > max_duration:
                    await _delete_form_answer_message(bot, message, session, can_delete_all_messages)
                    await send_current_form_question(bot, message.chat.id)
                    return True
                if await _booking_interval_conflicts_for_form(int(session["form_id"]), interval):
                    await _delete_form_answer_message(bot, message, session, can_delete_all_messages)
                    await send_current_form_question(bot, message.chat.id)
                    return True
            elif date_iso:
                try:
                    step = int(await db.get_setting("booking_slot_minutes", "60") or 60)
                except ValueError:
                    step = 60
                end_minutes = _time_to_minutes(answer) + max(15, step)
                end_time = f"{min(end_minutes, 24 * 60) // 60:02d}:{min(end_minutes, 24 * 60) % 60:02d}"
                if end_minutes >= 24 * 60:
                    end_time = "24:00"
                if await db.booking_conflicts(date_iso, answer, end_time):
                    await _delete_form_answer_message(bot, message, session, can_delete_all_messages)
                    await send_current_form_question(bot, message.chat.id)
                    return True
    else:
        answer = _message_answer_text(message)

    if not answer:
        await _upsert_form_message(
            bot,
            session,
            text="Пожалуйста, отправьте ответ текстом.",
            reply_markup=form_question_nav(bool(question["required"]), index > 0),
        )
        return True

    if len(answer) > 1500:
        await _upsert_form_message(
            bot,
            session,
            text="Ответ слишком длинный. Пожалуйста, сократите его до 1500 символов.",
            reply_markup=form_question_nav(bool(question["required"]), index > 0),
        )
        return True

    answers = dict(session["answers"])
    answers[str(questions[index]["id"])] = answer
    next_index = index + 1
    await db.update_form_session(
        message.chat.id,
        current_index=next_index,
        answers=answers,
        status="confirm" if next_index >= len(questions) else "active",
        keyboard_question_id=0,
    )
    await _delete_form_answer_message(bot, message, session, can_delete_all_messages)
    await send_current_form_question(bot, message.chat.id)
    return True


def _submission_chat_text(
    submission_id: int,
    form: dict,
    questions: list[dict],
    answers: dict[str, str],
    pricing_calculation: dict | None = None,
    selected_addons: list[dict] | None = None,
    currency: str = "₽",
) -> str:
    lines = [
        f"✅ Заявка №{submission_id} принята",
        "",
        str(form["name"]),
        "",
    ]
    for question in questions:
        value = answers.get(str(question["id"])) or "—"
        lines.append(f"{question['label']}: {value}")
    interval_summary = _booking_interval_summary(_booking_interval(questions, answers))
    if interval_summary:
        lines.extend(["", f"🕐 Интервал: {interval_summary}"])
    if selected_addons and not pricing_calculation:
        addon_text = _addons_text(selected_addons, currency)
        if addon_text:
            lines.extend(["", addon_text])
    pricing_text = _pricing_calculation_text(pricing_calculation)
    if pricing_text:
        lines.extend(["", pricing_text])
    lines.extend(
        [
            "",
            "Спасибо! Заявка сохранена. Я свяжусь с вами в этом чате.",
        ]
    )
    return "\n".join(lines)


async def render_admin_home() -> tuple[str, object]:
    enabled = await get_autoresponder_enabled()
    cooldown = int(await db.get_setting("cooldown_hours", "168") or 168)
    columns = int(await db.get_setting("menu_columns", "1") or 1)
    trigger_count = len(await get_menu_triggers())
    pricing_forms = await db.list_forms_with_pricing()
    active_pricing_count = sum(1 for item in pricing_forms if item.get("pricing_enabled"))
    connection = await db.latest_business_connection()

    if connection and connection["enabled"]:
        conn_text = "🟢 подключён"
        if not connection["can_reply"]:
            conn_text += " (нет права отвечать)"
        clean_form_text = (
            "🟢 ответы скрываются"
            if connection.get("can_delete_all_messages")
            else "⚪ ответы видны — включите «Удаление входящих»"
        )
    else:
        conn_text = "⚪ не подключён"
        clean_form_text = "⚪ недоступно"

    text = (
        "<b>Автоответчик Telegram Business</b>\n\n"
        f"Автоответ: {'включён' if enabled else 'выключен'}\n"
        f"Повторный автоответ после паузы: {cooldown} ч.\n"
        f"Кнопок в строке: {columns}\n"
        f"Фраз вызова меню: {trigger_count}\n"
        f"Тарифов с авторасчётом: {active_pricing_count}\n"
        f"Business-соединение: {conn_text}\n"
        f"Чистая форма: {clean_form_text}\n"
        f"Часовой пояс: {html.escape(settings.timezone_name)}\n\n"
        "Настройки меняются прямо здесь и сохраняются в SQLite."
    )
    return text, admin_main(enabled)


async def show_admin_home_message(message: Message) -> None:
    text, markup = await render_admin_home()
    await message.answer(text, reply_markup=markup)


async def show_admin_home_callback(callback: CallbackQuery) -> None:
    text, markup = await render_admin_home()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


async def load_connection_from_telegram(bot: Bot, connection_id: str) -> dict | None:
    try:
        connection = await bot.get_business_connection(business_connection_id=connection_id)
    except TelegramAPIError:
        logger.exception("Не удалось получить BusinessConnection %s", connection_id)
        return None

    can_reply = bool(connection.rights and connection.rights.can_reply)
    can_delete_all_messages = bool(
        connection.rights and connection.rights.can_delete_all_messages
    )
    await db.save_business_connection(
        connection_id=connection.id,
        owner_user_id=connection.user.id,
        user_chat_id=connection.user_chat_id,
        enabled=connection.is_enabled,
        can_reply=can_reply,
        can_delete_all_messages=can_delete_all_messages,
    )
    return await db.get_business_connection(connection_id)


@router.business_connection()
async def on_business_connection(event: BusinessConnection, bot: Bot) -> None:
    can_reply = bool(event.rights and event.rights.can_reply)
    can_delete_all_messages = bool(
        event.rights and event.rights.can_delete_all_messages
    )
    await db.save_business_connection(
        connection_id=event.id,
        owner_user_id=event.user.id,
        user_chat_id=event.user_chat_id,
        enabled=event.is_enabled,
        can_reply=can_reply,
        can_delete_all_messages=can_delete_all_messages,
    )

    status = "подключён" if event.is_enabled else "отключён"
    rights = "есть право отвечать" if can_reply else "НЕТ права отвечать"
    delete_rights = (
        "удаление входящих разрешено"
        if can_delete_all_messages
        else "удаление входящих НЕ разрешено"
    )
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(
                admin_id,
                f"Business-бот {status}. Статус: {rights}; {delete_rights}.",
            )
        except TelegramAPIError:
            logger.warning("Не удалось уведомить администратора %s", admin_id)


@router.business_message()
async def on_business_message(message: Message, bot: Bot) -> None:
    connection_id = message.business_connection_id
    if not connection_id or message.chat.type != ChatType.PRIVATE:
        return

    # Сообщения, отправленные самим business-ботом, не должны запускать автоответ снова.
    if message.sender_business_bot:
        return

    connection = await db.get_business_connection(connection_id)
    if not connection:
        connection = await load_connection_from_telegram(bot, connection_id)
    if not connection or not connection["enabled"]:
        return

    # Исходящие сообщения владельца Business-аккаунта тоже приходят как business_message.
    if message.from_user and message.from_user.id == connection["owner_user_id"]:
        return

    if not message.from_user or message.from_user.is_bot:
        return

    previous = await db.get_contact(message.chat.id)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    await db.upsert_contact(
        chat_id=message.chat.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        inbound_at=now_iso,
    )

    session = await db.get_form_session(message.chat.id)

    # Ручной вызов меню всегда имеет приоритет над cooldown и активной формой.
    if await is_menu_trigger(message.text):
        if not connection["can_reply"]:
            logger.warning("Нет права can_reply для ручного вызова меню в %s", connection_id)
            return
        edit_message_id = int(session["form_message_id"]) if session and session.get("form_message_id") else None
        await _delete_incoming_business_message(
            bot,
            business_connection_id=connection_id,
            message_id=message.message_id,
            allowed=bool(connection.get("can_delete_all_messages")),
        )
        menu_message_id = await _show_public_menu(
            bot,
            chat_id=message.chat.id,
            business_connection_id=connection_id,
            edit_message_id=edit_message_id,
        )
        if menu_message_id is not None and session:
            await db.delete_form_session(message.chat.id)
        return

    if session:
        if not connection["can_reply"]:
            logger.warning("Нет права can_reply для активной формы в BusinessConnection %s", connection_id)
            return
        if session["business_connection_id"] != connection_id:
            await db.update_form_session(
                message.chat.id, business_connection_id=connection_id
            )
            session["business_connection_id"] = connection_id
        await handle_form_message(
            message,
            bot,
            session,
            can_delete_all_messages=bool(connection.get("can_delete_all_messages")),
        )
        return

    if not await get_autoresponder_enabled():
        return

    cooldown_hours = int(await db.get_setting("cooldown_hours", "168") or 168)
    should_reply = previous is None
    if previous is not None:
        try:
            prev_dt = datetime.fromisoformat(previous["last_inbound_at"])
            should_reply = now - prev_dt >= timedelta(hours=cooldown_hours)
        except (TypeError, ValueError):
            should_reply = True

    if not should_reply:
        return

    if not connection["can_reply"]:
        logger.warning("Нет права can_reply для BusinessConnection %s", connection_id)
        return

    greeting = await db.get_setting("greeting", "Здравствуйте!") or "Здравствуйте!"
    sent_id = await _show_public_menu(
        bot,
        chat_id=message.chat.id,
        business_connection_id=connection_id,
        text=greeting,
    )
    if sent_id is not None:
        await db.mark_auto_reply(message.chat.id, now_iso)


@router.callback_query(F.data.startswith("pub:"))
async def on_public_button(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.data:
        return
    try:
        button_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return

    button = await db.get_button(button_id)
    if not button or not button["enabled"]:
        await callback.answer("Эта кнопка больше недоступна", show_alert=True)
        return

    if not isinstance(callback.message, Message) or not callback.message.business_connection_id:
        await callback.answer("Не удалось определить бизнес-чат", show_alert=True)
        return

    try:
        form = await db.get_bound_form(button_id, enabled_only=True)
        if form:
            await db.start_form_session(
                chat_id=callback.message.chat.id,
                business_connection_id=callback.message.business_connection_id,
                form_id=int(form["id"]),
                user_id=callback.from_user.id,
                username=callback.from_user.username,
                first_name=callback.from_user.first_name,
                last_name=callback.from_user.last_name,
                form_message_id=callback.message.message_id,
            )
            await db.mark_button_click(callback.message.chat.id, utc_now_iso())
            await send_current_form_question(bot, callback.message.chat.id)
            await callback.answer("Открываю заявку")
            return

        await bot.send_message(
            chat_id=callback.message.chat.id,
            business_connection_id=callback.message.business_connection_id,
            text=button["response"],
            parse_mode=None,
        )
        await db.mark_button_click(callback.message.chat.id, utc_now_iso())
        await callback.answer("Принято")
    except TelegramAPIError:
        logger.exception("Ошибка ответа на business callback")
        try:
            await callback.answer(
                "Не удалось отправить ответ. Возможно, истёк лимит Telegram Business на ответы.",
                show_alert=True,
            )
        except TelegramAPIError:
            pass


@router.callback_query(F.data == "cal:noop")
async def calendar_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("cal:busy:"))
async def calendar_busy(callback: CallbackQuery) -> None:
    await callback.answer("Эта дата полностью занята. Выберите другой день.", show_alert=True)


async def _calendar_session_question(callback: CallbackQuery, question_id: int) -> tuple[dict, dict] | None:
    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return None
    if session["status"] != "active":
        await callback.answer("Нет активного вопроса", show_alert=True)
        return None
    questions = await db.list_form_questions(int(session["form_id"]))
    index = int(session["current_index"])
    if index < 0 or index >= len(questions):
        await callback.answer("Нет активного вопроса", show_alert=True)
        return None
    question = questions[index]
    if int(question["id"]) != question_id or _question_input_type(question) != "date":
        await callback.answer("Этот календарь уже неактивен", show_alert=True)
        return None
    return session, question


@router.callback_query(F.data.startswith("cal:nav:"))
async def calendar_navigate(callback: CallbackQuery, bot: Bot) -> None:
    try:
        _, _, question_raw, ym = (callback.data or "").split(":", 3)
        question_id = int(question_raw)
        year, month = [int(x) for x in ym.split("-", 1)]
        datetime(year, month, 1)
    except (ValueError, TypeError):
        await callback.answer("Некорректная дата", show_alert=True)
        return
    data = await _calendar_session_question(callback, question_id)
    if not data or not isinstance(callback.message, Message):
        return
    await send_current_form_question(
        bot, callback.message.chat.id, calendar_year=year, calendar_month=month
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cal:today:"))
async def calendar_today(callback: CallbackQuery, bot: Bot) -> None:
    try:
        question_id = int((callback.data or "").rsplit(":", 1)[1])
    except (ValueError, TypeError):
        await callback.answer("Некорректная дата", show_alert=True)
        return
    data = await _calendar_session_question(callback, question_id)
    if not data or not isinstance(callback.message, Message):
        return
    session, question = data
    today_date = local_today()
    if await _date_fully_busy(today_date.isoformat()):
        await callback.answer("Сегодня дата полностью занята", show_alert=True)
        return
    selected = today_date.strftime("%d.%m.%Y")
    answers = dict(session["answers"])
    answers[str(question["id"])] = selected
    questions = await db.list_form_questions(int(session["form_id"]))
    next_index = int(session["current_index"]) + 1
    await db.update_form_session(
        callback.message.chat.id,
        current_index=next_index,
        answers=answers,
        status="confirm" if next_index >= len(questions) else "active",
        keyboard_question_id=0,
    )
    await send_current_form_question(bot, callback.message.chat.id)
    await callback.answer(f"Дата: {selected}")


@router.callback_query(F.data.startswith("cal:day:"))
async def calendar_select_day(callback: CallbackQuery, bot: Bot) -> None:
    try:
        _, _, question_raw, iso = (callback.data or "").split(":", 3)
        question_id = int(question_raw)
        selected_date = datetime.strptime(iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        await callback.answer("Некорректная дата", show_alert=True)
        return
    data = await _calendar_session_question(callback, question_id)
    if not data or not isinstance(callback.message, Message):
        return
    session, question = data
    if await _date_fully_busy(selected_date.isoformat()):
        await callback.answer("Эта дата полностью занята", show_alert=True)
        return
    selected = selected_date.strftime("%d.%m.%Y")
    answers = dict(session["answers"])
    answers[str(question["id"])] = selected
    questions = await db.list_form_questions(int(session["form_id"]))
    next_index = int(session["current_index"]) + 1
    await db.update_form_session(
        callback.message.chat.id,
        current_index=next_index,
        answers=answers,
        status="confirm" if next_index >= len(questions) else "active",
        keyboard_question_id=0,
    )
    await send_current_form_question(bot, callback.message.chat.id)
    await callback.answer(f"Дата: {selected}")


async def _time_session_question(callback: CallbackQuery, question_id: int) -> tuple[dict, dict, list[dict]] | None:
    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return None
    if session["status"] != "active":
        await callback.answer("Нет активного вопроса", show_alert=True)
        return None
    questions = await db.list_form_questions(int(session["form_id"]))
    index = int(session["current_index"])
    if index < 0 or index >= len(questions):
        await callback.answer("Нет активного вопроса", show_alert=True)
        return None
    question = questions[index]
    if int(question["id"]) != question_id or _question_input_type(question) != "time":
        await callback.answer("Эта кнопка времени уже неактивна", show_alert=True)
        return None
    return session, question, questions


@router.callback_query(F.data.startswith("time:busy:"))
async def time_busy(callback: CallbackQuery) -> None:
    await callback.answer("Это время уже занято. Выберите другой вариант.", show_alert=True)


@router.callback_query(F.data.startswith("time:pick:"))
async def time_pick(callback: CallbackQuery, bot: Bot) -> None:
    try:
        _, _, question_raw, compact = (callback.data or "").split(":", 3)
        question_id = int(question_raw)
        if len(compact) != 4 or not compact.isdigit():
            raise ValueError
        selected = _parse_time_answer(f"{compact[:2]}:{compact[2:]}")
        if not selected:
            raise ValueError
    except (ValueError, TypeError):
        await callback.answer("Некорректное время", show_alert=True)
        return
    data = await _time_session_question(callback, question_id)
    if not data or not isinstance(callback.message, Message):
        return
    session, question, questions = data
    answers = dict(session["answers"])
    answers[str(question["id"])] = selected
    interval = _booking_interval(questions, answers)

    if _is_end_time_question(question) and interval and interval.get("start_time"):
        max_duration = await _max_booking_duration_minutes()
        if int(interval.get("duration_minutes") or 0) > max_duration:
            await callback.answer(
                f"Максимальный интервал — {max_duration // 60} ч.", show_alert=True
            )
            return
        if await _booking_interval_conflicts_for_form(int(session["form_id"]), interval):
            await callback.answer("Этот интервал пересекается с занятой бронью", show_alert=True)
            await send_current_form_question(bot, callback.message.chat.id)
            return
    else:
        date_iso = _selected_date_iso(questions, answers)
        existing_end = _find_end_time(questions, answers)
        if existing_end and interval:
            max_duration = await _max_booking_duration_minutes()
            if int(interval.get("duration_minutes") or 0) > max_duration:
                await callback.answer(
                    f"Максимальный интервал — {max_duration // 60} ч.", show_alert=True
                )
                return
            if await _booking_interval_conflicts_for_form(int(session["form_id"]), interval):
                await callback.answer("Этот интервал пересекается с занятой бронью", show_alert=True)
                await send_current_form_question(bot, callback.message.chat.id)
                return
        elif date_iso:
            try:
                step = int(await db.get_setting("booking_slot_minutes", "60") or 60)
            except ValueError:
                step = 60
            end_minutes = _time_to_minutes(selected) + max(15, step)
            end_time = "24:00" if end_minutes >= 24 * 60 else _minutes_to_time(end_minutes)
            if await db.booking_conflicts(date_iso, selected, end_time):
                await callback.answer("Это время уже занято", show_alert=True)
                await send_current_form_question(bot, callback.message.chat.id)
                return
    next_index = int(session["current_index"]) + 1
    await db.update_form_session(
        callback.message.chat.id,
        current_index=next_index,
        answers=answers,
        status="confirm" if next_index >= len(questions) else "active",
        keyboard_question_id=0,
    )
    await send_current_form_question(bot, callback.message.chat.id)
    if _is_end_time_question(question) and interval and interval.get("overnight"):
        await callback.answer(f"Окончание: {selected} (+1 день)")
    else:
        await callback.answer(f"Время: {selected}")


@router.callback_query(F.data.startswith("guests:pick:"))
async def guest_count_pick(callback: CallbackQuery, bot: Bot) -> None:
    try:
        _, _, question_raw, option_raw = (callback.data or "").split(":", 3)
        question_id = int(question_raw)
        option_index = int(option_raw)
        selected = GUEST_COUNT_OPTIONS[option_index]
    except (ValueError, TypeError, IndexError):
        await callback.answer("Некорректный вариант", show_alert=True)
        return

    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return
    if session.get("status") != "active":
        await callback.answer("Нет активного вопроса", show_alert=True)
        return
    questions = await db.list_form_questions(int(session["form_id"]))
    index = int(session["current_index"])
    if index < 0 or index >= len(questions):
        await callback.answer("Нет активного вопроса", show_alert=True)
        return
    question = questions[index]
    if int(question["id"]) != question_id or _question_input_type(question) != "guest_count":
        await callback.answer("Эта кнопка уже неактивна", show_alert=True)
        return

    answers = dict(session["answers"])
    answers[str(question["id"])] = selected
    next_index = index + 1
    await db.update_form_session(
        callback.message.chat.id,
        current_index=next_index,
        answers=answers,
        status="confirm" if next_index >= len(questions) else "active",
        keyboard_question_id=0,
    )
    await send_current_form_question(bot, callback.message.chat.id)
    await callback.answer(f"Гости: {selected}")


@router.callback_query(F.data == "form:menu")
async def form_main_menu(callback: CallbackQuery, bot: Bot) -> None:
    if not isinstance(callback.message, Message) or not callback.message.business_connection_id:
        await callback.answer("Не удалось открыть меню", show_alert=True)
        return

    session = await db.get_form_session(callback.message.chat.id)
    if session and session.get("form_message_id"):
        if int(session["form_message_id"]) != int(callback.message.message_id):
            await callback.answer("Это старая кнопка формы", show_alert=True)
            return

    menu_message_id = await _show_public_menu(
        bot,
        chat_id=callback.message.chat.id,
        business_connection_id=callback.message.business_connection_id,
        edit_message_id=callback.message.message_id,
    )
    if menu_message_id is None:
        await callback.answer("Не удалось открыть меню", show_alert=True)
        return
    await db.delete_form_session(callback.message.chat.id)
    await callback.answer("Главное меню")


@router.callback_query(F.data == "form:cancel")
async def form_cancel(callback: CallbackQuery, bot: Bot) -> None:
    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return
    await _upsert_form_message(bot, session, text="Заявка отменена.", reply_markup=None)
    await db.delete_form_session(callback.message.chat.id)
    await callback.answer("Отменено")


@router.callback_query(F.data == "form:skip")
async def form_skip(callback: CallbackQuery, bot: Bot) -> None:
    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return
    if session["status"] != "active":
        await callback.answer("Нет активного вопроса", show_alert=True)
        return
    questions = await db.list_form_questions(int(session["form_id"]))
    index = int(session["current_index"])
    if index < 0 or index >= len(questions):
        await callback.answer("Нет активного вопроса", show_alert=True)
        return
    question = questions[index]
    if question["required"]:
        await callback.answer("Этот вопрос обязателен", show_alert=True)
        return
    answers = dict(session["answers"])
    answers.pop(str(question["id"]), None)
    next_index = index + 1
    await db.update_form_session(
        callback.message.chat.id,
        current_index=next_index,
        answers=answers,
        status="confirm" if next_index >= len(questions) else "active",
        keyboard_question_id=0,
    )
    await send_current_form_question(bot, callback.message.chat.id)
    await callback.answer("Пропущено")


@router.callback_query(F.data.startswith("form:addon_toggle:"))
async def form_addon_toggle(callback: CallbackQuery, bot: Bot) -> None:
    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return
    if session.get("status") != "addons":
        await callback.answer("Выбор услуг уже завершён", show_alert=True)
        return
    try:
        addon_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная услуга", show_alert=True)
        return
    addon = await db.get_form_addon(addon_id)
    if not addon or int(addon.get("form_id") or 0) != int(session["form_id"]) or not addon.get("enabled"):
        await callback.answer("Услуга больше недоступна", show_alert=True)
        return
    selected = [int(x) for x in session.get("selected_addon_ids") or []]
    if addon_id in selected:
        selected = [x for x in selected if x != addon_id]
        notice = "Услуга убрана"
    else:
        selected.append(addon_id)
        notice = "Услуга добавлена"
    await db.update_form_session(callback.message.chat.id, selected_addon_ids=selected)
    await send_current_form_question(bot, callback.message.chat.id)
    await callback.answer(notice)


@router.callback_query(F.data == "form:addons_clear")
async def form_addons_clear(callback: CallbackQuery, bot: Bot) -> None:
    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return
    await db.update_form_session(callback.message.chat.id, selected_addon_ids=[])
    await send_current_form_question(bot, callback.message.chat.id)
    await callback.answer("Дополнительные услуги убраны")


@router.callback_query(F.data == "form:addons_done")
async def form_addons_done(callback: CallbackQuery, bot: Bot) -> None:
    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return
    await db.update_form_session(
        callback.message.chat.id, addons_confirmed=True, status="active"
    )
    await send_current_form_question(bot, callback.message.chat.id)
    await callback.answer("Дополнительные услуги сохранены")


@router.callback_query(F.data == "form:addons_edit")
async def form_addons_edit(callback: CallbackQuery, bot: Bot) -> None:
    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return
    await db.update_form_session(
        callback.message.chat.id, current_index=int(session.get("current_index") or 0),
        addons_confirmed=False, status="addons"
    )
    await send_current_form_question(bot, callback.message.chat.id)
    await callback.answer("Можно изменить дополнительные услуги")


@router.callback_query(F.data == "form:back")
async def form_back(callback: CallbackQuery, bot: Bot) -> None:
    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return
    questions = await db.list_form_questions(int(session["form_id"]))
    if not questions:
        await callback.answer("В форме нет вопросов", show_alert=True)
        return
    if session["status"] in {"confirm", "addons"}:
        target = len(questions) - 1
    else:
        target = int(session["current_index"]) - 1
    if target < 0:
        await callback.answer("Это первый вопрос", show_alert=True)
        return
    await db.update_form_session(
        callback.message.chat.id, current_index=target, status="active", keyboard_question_id=0,
        addons_confirmed=False
    )
    await send_current_form_question(bot, callback.message.chat.id)
    await callback.answer()


@router.callback_query(F.data == "form:edit")
async def form_edit_answers(callback: CallbackQuery, bot: Bot) -> None:
    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return
    await db.update_form_session(
        callback.message.chat.id, current_index=0, status="active", keyboard_question_id=0,
        addons_confirmed=False
    )
    await send_current_form_question(bot, callback.message.chat.id)
    await callback.answer("Можно изменить ответы")


@router.callback_query(F.data == "form:submit")
async def form_submit(callback: CallbackQuery, bot: Bot) -> None:
    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return
    if session["status"] != "confirm":
        await callback.answer("Заявка ещё не завершена", show_alert=True)
        return
    form = await db.get_form(int(session["form_id"]))
    questions = await db.list_form_questions(int(session["form_id"]))
    if not form:
        await _upsert_form_message(bot, session, text="Форма больше недоступна.", reply_markup=None)
        await db.delete_form_session(callback.message.chat.id)
        await callback.answer("Форма больше недоступна", show_alert=True)
        return

    for index, question in enumerate(questions):
        if question["required"] and not session["answers"].get(str(question["id"])):
            await db.update_form_session(
                callback.message.chat.id, current_index=index, status="active"
            )
            await callback.answer("Нужно заполнить обязательный вопрос", show_alert=True)
            await send_current_form_question(bot, callback.message.chat.id)
            return

    booking_interval = _booking_interval(questions, session["answers"])
    if booking_interval and await _booking_interval_conflicts_for_form(int(form["id"]), booking_interval):
        target_index = 0
        if booking_interval.get("start_time"):
            for idx, question in enumerate(questions):
                if _question_input_type(question) == "time" and not _is_end_time_question(question):
                    target_index = idx
                    break
        else:
            for idx, question in enumerate(questions):
                if _question_input_type(question) == "date":
                    target_index = idx
                    break
        await db.update_form_session(
            callback.message.chat.id, current_index=target_index, status="active"
        )
        await callback.answer(
            "Выбранный интервал уже занят. Выберите другую дату или время.", show_alert=True
        )
        await send_current_form_question(bot, callback.message.chat.id)
        return

    selected_addons = await _selected_addon_rows(
        int(form["id"]), session.get("selected_addon_ids") or []
    )
    pricing_calculation = await _calculate_form_pricing(
        int(form["id"]), questions, session["answers"], session.get("selected_addon_ids") or []
    )
    calculated_amount = int(pricing_calculation.get("amount") or 0) if pricing_calculation else 0
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    pricing_details = pricing_calculation or {
        "currency": currency,
        "addons_total": sum(max(0, int(item.get("amount") or 0)) for item in selected_addons),
        "addons": [
            {"id": int(item["id"]), "name": str(item["name"]), "amount": max(0, int(item.get("amount") or 0))}
            for item in selected_addons
        ],
    }
    submission_id = await db.create_form_submission(
        session,
        str(form["name"]),
        calculated_amount=calculated_amount,
        pricing_details=pricing_details,
    )
    await _upsert_form_message(
        bot,
        session,
        text=_submission_chat_text(
            submission_id, form, questions, session["answers"], pricing_calculation,
            selected_addons, currency
        ),
        reply_markup=None,
    )
    await db.delete_form_session(callback.message.chat.id)
    await callback.answer("Заявка сохранена в этом чате")


@router.message(Command("start", "admin"))
async def admin_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    await show_admin_home_message(message)


@router.callback_query(F.data == "adm:home")
async def admin_home(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await show_admin_home_callback(callback)


@router.callback_query(F.data == "adm:toggle")
async def admin_toggle(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    enabled = await get_autoresponder_enabled()
    await db.set_setting("autoresponder_enabled", "0" if enabled else "1")
    await show_admin_home_callback(callback)


@router.callback_query(F.data == "adm:greeting")
async def admin_greeting(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    current = await db.get_setting("greeting", "") or ""
    await state.set_state(AdminStates.greeting)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Отправьте новое приветствие одним сообщением.\n\n"
            f"<b>Сейчас:</b>\n{html.escape(current)}"
        )
    await callback.answer()


@router.message(AdminStates.greeting)
async def admin_greeting_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст не должен быть пустым.")
        return
    await db.set_setting("greeting", text)
    await state.clear()
    await message.answer("Приветствие сохранено.")
    await show_admin_home_message(message)


@router.callback_query(F.data == "adm:cooldown")
async def admin_cooldown(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    current = await db.get_setting("cooldown_hours", "168")
    await state.set_state(AdminStates.cooldown)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"Сейчас интервал: <b>{current} ч.</b>\n"
            "Отправьте новое число часов (например 24, 72 или 168)."
        )
    await callback.answer()


@router.message(AdminStates.cooldown)
async def admin_cooldown_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    try:
        value = int((message.text or "").strip())
        if not 1 <= value <= 8760:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое число от 1 до 8760.")
        return
    await db.set_setting("cooldown_hours", str(value))
    await state.clear()
    await message.answer(f"Интервал сохранён: {value} ч.")
    await show_admin_home_message(message)


@router.callback_query(F.data == "adm:menu_triggers")
async def admin_menu_triggers(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    current = await get_menu_triggers()
    await state.set_state(AdminStates.menu_triggers)
    if isinstance(callback.message, Message):
        shown = "\n".join(f"• {html.escape(item)}" for item in current)
        await callback.message.answer(
            "<b>Фразы вызова главного меню</b>\n\n"
            f"Сейчас:\n{shown}\n\n"
            "Отправьте новый список — по одной фразе в каждой строке. "
            "Регистр не важен. Например:\n<code>/menu\nменю\nзаявка</code>"
        )
    await callback.answer()


@router.message(AdminStates.menu_triggers)
async def admin_menu_triggers_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    raw = (message.text or "").strip()
    values = _parse_menu_triggers(raw)
    if not values:
        await message.answer("Нужно указать хотя бы одну фразу вызова меню.")
        return
    if len(values) > 20:
        await message.answer("Можно указать не более 20 фраз.")
        return
    if any(len(value) > 64 for value in values):
        await message.answer("Каждая фраза должна быть не длиннее 64 символов.")
        return
    await db.set_setting("menu_triggers", "\n".join(values))
    await state.clear()
    await message.answer(
        "Фразы вызова меню сохранены:\n" + "\n".join(f"• {html.escape(v)}" for v in values)
    )
    await show_admin_home_message(message)


@router.callback_query(F.data == "adm:grid")
async def admin_grid(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    current = await db.get_setting("menu_columns", "1")
    await state.set_state(AdminStates.grid_columns)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"Сейчас кнопок в строке: <b>{current}</b>. Отправьте 1, 2 или 3."
        )
    await callback.answer()


@router.message(AdminStates.grid_columns)
async def admin_grid_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    try:
        value = int((message.text or "").strip())
        if value not in {1, 2, 3}:
            raise ValueError
    except ValueError:
        await message.answer("Введите 1, 2 или 3.")
        return
    await db.set_setting("menu_columns", str(value))
    await state.clear()
    await message.answer("Сетка меню сохранена.")
    await show_admin_home_message(message)


@router.callback_query(F.data == "adm:buttons")
async def admin_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    buttons = await db.list_buttons()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "<b>Конструктор кнопок</b>\n\n"
            "Нажмите кнопку для редактирования или добавьте новую.",
            reply_markup=admin_buttons_list(buttons),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:btn:"))
async def admin_button_open(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    button_id = int(callback.data.rsplit(":", 1)[1])
    button = await db.get_button(button_id)
    if not button:
        await callback.answer("Кнопка не найдена", show_alert=True)
        return
    text, markup = await render_admin_button(button)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "adm:add")
async def admin_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.add_title)
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите название новой кнопки:")
    await callback.answer()


@router.message(AdminStates.add_title)
async def admin_add_title(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    title = (message.text or "").strip()
    if not title or len(title) > 64:
        await message.answer("Название должно быть от 1 до 64 символов.")
        return
    await state.update_data(title=title)
    await state.set_state(AdminStates.add_response)
    await message.answer("Теперь отправьте текст ответа при нажатии:")


@router.message(AdminStates.add_response)
async def admin_add_response(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    response = (message.text or "").strip()
    if not response:
        await message.answer("Ответ не должен быть пустым.")
        return
    data = await state.get_data()
    button_id = await db.add_button(data["title"], response)
    await state.clear()
    await message.answer(f"Кнопка создана, ID {button_id}.")
    buttons = await db.list_buttons()
    await message.answer("Конструктор кнопок:", reply_markup=admin_buttons_list(buttons))


@router.callback_query(F.data.startswith("adm:title:"))
async def admin_edit_title(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    button_id = int(callback.data.rsplit(":", 1)[1])
    await state.update_data(button_id=button_id)
    await state.set_state(AdminStates.edit_title)
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите новое название кнопки:")
    await callback.answer()


@router.message(AdminStates.edit_title)
async def admin_edit_title_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    title = (message.text or "").strip()
    if not title or len(title) > 64:
        await message.answer("Название должно быть от 1 до 64 символов.")
        return
    data = await state.get_data()
    await db.update_button_field(int(data["button_id"]), "title", title)
    await state.clear()
    await message.answer("Название сохранено.")


@router.callback_query(F.data.startswith("adm:response:"))
async def admin_edit_response(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    button_id = int(callback.data.rsplit(":", 1)[1])
    await state.update_data(button_id=button_id)
    await state.set_state(AdminStates.edit_response)
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите новый текст ответа:")
    await callback.answer()


@router.message(AdminStates.edit_response)
async def admin_edit_response_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    response = (message.text or "").strip()
    if not response:
        await message.answer("Ответ не должен быть пустым.")
        return
    data = await state.get_data()
    await db.update_button_field(int(data["button_id"]), "response", response)
    await state.clear()
    await message.answer("Ответ сохранён.")


@router.callback_query(F.data.startswith("adm:position:"))
async def admin_edit_position(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    button_id = int(callback.data.rsplit(":", 1)[1])
    await state.update_data(button_id=button_id)
    await state.set_state(AdminStates.edit_position)
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите номер позиции (целое число от 1 до 999):")
    await callback.answer()


@router.message(AdminStates.edit_position)
async def admin_edit_position_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    try:
        position = int((message.text or "").strip())
        if not 1 <= position <= 999:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое число от 1 до 999.")
        return
    data = await state.get_data()
    await db.update_button_field(int(data["button_id"]), "position", position)
    await state.clear()
    await message.answer("Позиция сохранена.")


@router.callback_query(F.data.startswith("adm:enable:"))
async def admin_enable_toggle(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    button_id = int(callback.data.rsplit(":", 1)[1])
    button = await db.get_button(button_id)
    if not button:
        await callback.answer("Кнопка не найдена", show_alert=True)
        return
    await db.update_button_field(button_id, "enabled", 0 if button["enabled"] else 1)
    button = await db.get_button(button_id)
    if isinstance(callback.message, Message) and button:
        text, markup = await render_admin_button(button)
        await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("adm:delete:"))
async def admin_delete(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    button_id = int(callback.data.rsplit(":", 1)[1])
    button = await db.get_button(button_id)
    if not button:
        await callback.answer("Кнопка не найдена", show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"Удалить кнопку <b>{html.escape(button['title'])}</b>?",
            reply_markup=delete_confirm(button_id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:delete_confirm:"))
async def admin_delete_confirm(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    button_id = int(callback.data.rsplit(":", 1)[1])
    await db.delete_button(button_id)
    buttons = await db.list_buttons()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Кнопка удалена.\n\n<b>Конструктор кнопок</b>",
            reply_markup=admin_buttons_list(buttons),
        )
    await callback.answer("Удалено")


async def _show_admin_form(callback: CallbackQuery, form_id: int) -> None:
    form = await db.get_form(form_id)
    if not form:
        await callback.answer("Форма не найдена", show_alert=True)
        return
    button_ids = set(await db.list_form_button_ids(form_id))
    buttons = await db.list_buttons()
    names = [b["title"] for b in buttons if int(b["id"]) in button_ids]
    bound = ", ".join(html.escape(name) for name in names) if names else "не привязана"
    status = "включена" if form["enabled"] else "выключена"
    text = (
        f"<b>📝 {html.escape(form['name'])}</b>\n\n"
        f"Статус: {status}\n"
        f"Вопросов: {form['question_count']}\n"
        f"Кнопок: {form['button_count']}\n"
        f"Привязка: {bound}\n\n"
        "При нажатии на привязанную кнопку пользователь проходит вопросы по очереди, "
        "проверяет ответы и отправляет готовую заявку."
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=admin_form_edit(form))
    await callback.answer()


@router.callback_query(F.data == "adm:forms")
async def admin_forms(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    forms = await db.list_forms()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "<b>Конструктор форм</b>\n\n"
            "Форма запускается после нажатия на привязанную кнопку. "
            "Звёздочкой в списке вопросов отмечены обязательные поля.",
            reply_markup=admin_forms_list(forms),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:form:") )
async def admin_form_open(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        form_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Некорректная форма", show_alert=True)
        return
    await _show_admin_form(callback, form_id)


@router.callback_query(F.data == "adm:form_add")
async def admin_form_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.form_add_name)
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите название новой формы:")
    await callback.answer()


@router.message(AdminStates.form_add_name)
async def admin_form_add_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    name = (message.text or "").strip()
    if not name or len(name) > 80:
        await message.answer("Название должно быть от 1 до 80 символов.")
        return
    form_id = await db.add_form(name)
    await state.clear()
    await message.answer(
        f"Форма «{html.escape(name)}» создана. Теперь добавьте вопросы и привяжите кнопку."
    )
    form = await db.get_form(form_id)
    await message.answer("Настройки формы:", reply_markup=admin_form_edit(form))


@router.callback_query(F.data.startswith("adm:form_name:"))
async def admin_form_rename(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    form_id = int(callback.data.rsplit(":", 1)[1])
    form = await db.get_form(form_id)
    if not form:
        await callback.answer("Форма не найдена", show_alert=True)
        return
    await state.update_data(form_id=form_id)
    await state.set_state(AdminStates.form_edit_name)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"Введите новое название формы. Сейчас: <b>{html.escape(form['name'])}</b>"
        )
    await callback.answer()


@router.message(AdminStates.form_edit_name)
async def admin_form_rename_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    name = (message.text or "").strip()
    if not name or len(name) > 80:
        await message.answer("Название должно быть от 1 до 80 символов.")
        return
    data = await state.get_data()
    await db.update_form_field(int(data["form_id"]), "name", name)
    await state.clear()
    await message.answer("Название формы сохранено.")


@router.callback_query(F.data.startswith("adm:form_enable:"))
async def admin_form_enable(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    form_id = int(callback.data.rsplit(":", 1)[1])
    form = await db.get_form(form_id)
    if not form:
        await callback.answer("Форма не найдена", show_alert=True)
        return
    await db.update_form_field(form_id, "enabled", 0 if form["enabled"] else 1)
    await _show_admin_form(callback, form_id)


@router.callback_query(F.data.startswith("adm:form_delete:"))
async def admin_form_delete(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    form_id = int(callback.data.rsplit(":", 1)[1])
    form = await db.get_form(form_id)
    if not form:
        await callback.answer("Форма не найдена", show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"Удалить форму <b>{html.escape(form['name'])}</b> вместе с вопросами?",
            reply_markup=admin_form_delete_confirm(form_id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:form_delete_yes:"))
async def admin_form_delete_yes(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    form_id = int(callback.data.rsplit(":", 1)[1])
    await db.delete_form(form_id)
    forms = await db.list_forms()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Форма удалена.\n\n<b>Конструктор форм</b>",
            reply_markup=admin_forms_list(forms),
        )
    await callback.answer("Удалено")


@router.callback_query(F.data.startswith("adm:form_questions:"))
async def admin_form_questions_open(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    form_id = int(callback.data.rsplit(":", 1)[1])
    form = await db.get_form(form_id)
    if not form:
        await callback.answer("Форма не найдена", show_alert=True)
        return
    questions = await db.list_form_questions(form_id)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"<b>Вопросы: {html.escape(form['name'])}</b>\n\n"
            "* — обязательный вопрос. Порядок определяется номером позиции.",
            reply_markup=admin_form_questions(form_id, questions),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:q_add:"))
async def admin_question_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    form_id = int(callback.data.rsplit(":", 1)[1])
    if not await db.get_form(form_id):
        await callback.answer("Форма не найдена", show_alert=True)
        return
    await state.update_data(form_id=form_id)
    await state.set_state(AdminStates.question_add_label)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Введите короткое название поля, которое вы увидите в готовой заявке.\n"
            "Например: <b>Дата</b>, <b>Телефон</b>, <b>Количество гостей</b>."
        )
    await callback.answer()


@router.message(AdminStates.question_add_label)
async def admin_question_add_label(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    label = (message.text or "").strip()
    if not label or len(label) > 80:
        await message.answer("Название поля должно быть от 1 до 80 символов.")
        return
    await state.update_data(label=label)
    await state.set_state(AdminStates.question_add_prompt)
    await message.answer("Теперь напишите сам вопрос пользователю:")


@router.message(AdminStates.question_add_prompt)
async def admin_question_add_prompt(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    prompt = (message.text or "").strip()
    if not prompt or len(prompt) > 1000:
        await message.answer("Вопрос должен быть от 1 до 1000 символов.")
        return
    data = await state.get_data()
    question_id = await db.add_form_question(
        int(data["form_id"]), str(data["label"]), prompt, required=True
    )
    await state.clear()
    question = await db.get_form_question(question_id)
    await message.answer(
        "Вопрос добавлен как обязательный. При необходимости переключите обязательность ниже.",
        reply_markup=admin_question_edit(question),
    )


def _admin_question_text(question: dict) -> str:
    required = "да" if question["required"] else "нет"
    type_name = QUESTION_TYPE_NAMES.get(_question_input_type(question), "⌨️ Текст")
    return (
        f"<b>{html.escape(question['label'])}</b>\n\n"
        f"Позиция: {question['position']}\n"
        f"Обязательный: {required}\n"
        f"Тип поля: {type_name}\n\n"
        f"<b>Вопрос пользователю:</b>\n{html.escape(question['prompt'])}"
    )


@router.callback_query(F.data.startswith("adm:q:"))
async def admin_question_open(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    question_id = int(callback.data.rsplit(":", 1)[1])
    question = await db.get_form_question(question_id)
    if not question:
        await callback.answer("Вопрос не найден", show_alert=True)
        return
    text = _admin_question_text(question)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=admin_question_edit(question))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:q_label:"))
async def admin_question_label(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    question_id = int(callback.data.rsplit(":", 1)[1])
    question = await db.get_form_question(question_id)
    if not question:
        await callback.answer("Вопрос не найден", show_alert=True)
        return
    await state.update_data(question_id=question_id)
    await state.set_state(AdminStates.question_edit_label)
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите новое короткое название поля:")
    await callback.answer()


@router.message(AdminStates.question_edit_label)
async def admin_question_label_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    label = (message.text or "").strip()
    if not label or len(label) > 80:
        await message.answer("Название поля должно быть от 1 до 80 символов.")
        return
    data = await state.get_data()
    await db.update_form_question_field(int(data["question_id"]), "label", label)
    await state.clear()
    await message.answer("Название поля сохранено.")


@router.callback_query(F.data.startswith("adm:q_prompt:"))
async def admin_question_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    question_id = int(callback.data.rsplit(":", 1)[1])
    question = await db.get_form_question(question_id)
    if not question:
        await callback.answer("Вопрос не найден", show_alert=True)
        return
    await state.update_data(question_id=question_id)
    await state.set_state(AdminStates.question_edit_prompt)
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите новый текст вопроса пользователю:")
    await callback.answer()


@router.message(AdminStates.question_edit_prompt)
async def admin_question_prompt_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    prompt = (message.text or "").strip()
    if not prompt or len(prompt) > 1000:
        await message.answer("Вопрос должен быть от 1 до 1000 символов.")
        return
    data = await state.get_data()
    await db.update_form_question_field(int(data["question_id"]), "prompt", prompt)
    await state.clear()
    await message.answer("Текст вопроса сохранён.")


@router.callback_query(F.data.startswith("adm:q_type_menu:"))
async def admin_question_type_menu(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    question_id = int((callback.data or "").rsplit(":", 1)[1])
    question = await db.get_form_question(question_id)
    if not question:
        await callback.answer("Вопрос не найден", show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Выберите тип ответа для этого вопроса:\n\n"
            "⌨️ Текст — обычный ответ\n"
            "📅 Дата — календарь + проверка занятости\n"
            "🕐 Время — свободные интервалы кнопками + ручной ввод\n"
            "📱 Контакт — ручной ввод телефона с проверкой; Telegram-контакт тоже принимается\n"
            "👥 Гости — выбор диапазона количества гостей кнопками + ручной ввод числа",
            reply_markup=admin_question_type(question_id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:q_type:"))
async def admin_question_type_set(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        _, _, question_raw, input_type = (callback.data or "").split(":", 3)
        question_id = int(question_raw)
    except (ValueError, TypeError):
        await callback.answer("Некорректные данные", show_alert=True)
        return
    if input_type not in {"text", "date", "time", "contact", "guest_count"}:
        await callback.answer("Неизвестный тип", show_alert=True)
        return
    question = await db.get_form_question(question_id)
    if not question:
        await callback.answer("Вопрос не найден", show_alert=True)
        return
    await db.update_form_question_field(question_id, "input_type", input_type)
    question = await db.get_form_question(question_id)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            _admin_question_text(question), reply_markup=admin_question_edit(question)
        )
    await callback.answer("Тип поля сохранён")


@router.callback_query(F.data.startswith("adm:q_pos:"))
async def admin_question_position(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    question_id = int(callback.data.rsplit(":", 1)[1])
    question = await db.get_form_question(question_id)
    if not question:
        await callback.answer("Вопрос не найден", show_alert=True)
        return
    await state.update_data(question_id=question_id)
    await state.set_state(AdminStates.question_edit_position)
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите позицию вопроса от 1 до 999:")
    await callback.answer()


@router.message(AdminStates.question_edit_position)
async def admin_question_position_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    try:
        value = int((message.text or "").strip())
        if not 1 <= value <= 999:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое число от 1 до 999.")
        return
    data = await state.get_data()
    await db.update_form_question_field(int(data["question_id"]), "position", value)
    await state.clear()
    await message.answer("Позиция вопроса сохранена.")


@router.callback_query(F.data.startswith("adm:q_required:"))
async def admin_question_required(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    question_id = int(callback.data.rsplit(":", 1)[1])
    question = await db.get_form_question(question_id)
    if not question:
        await callback.answer("Вопрос не найден", show_alert=True)
        return
    await db.update_form_question_field(
        question_id, "required", 0 if question["required"] else 1
    )
    question = await db.get_form_question(question_id)
    text = _admin_question_text(question)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=admin_question_edit(question))
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("adm:q_delete:"))
async def admin_question_delete(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    question_id = int(callback.data.rsplit(":", 1)[1])
    question = await db.get_form_question(question_id)
    if not question:
        await callback.answer("Вопрос не найден", show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"Удалить вопрос <b>{html.escape(question['label'])}</b>?",
            reply_markup=admin_question_delete_confirm(question),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:q_delete_yes:"))
async def admin_question_delete_yes(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    question_id = int(callback.data.rsplit(":", 1)[1])
    question = await db.get_form_question(question_id)
    if not question:
        await callback.answer("Вопрос уже удалён", show_alert=True)
        return
    form_id = int(question["form_id"])
    await db.delete_form_question(question_id)
    questions = await db.list_form_questions(form_id)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Вопрос удалён.", reply_markup=admin_form_questions(form_id, questions)
        )
    await callback.answer("Удалено")


@router.callback_query(F.data.startswith("adm:form_bind:"))
async def admin_form_bind(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    form_id = int(callback.data.rsplit(":", 1)[1])
    form = await db.get_form(form_id)
    if not form:
        await callback.answer("Форма не найдена", show_alert=True)
        return
    buttons = await db.list_buttons()
    bound_ids = set(await db.list_form_button_ids(form_id))
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"<b>Кнопки для формы «{html.escape(form['name'])}»</b>\n\n"
            "Нажмите кнопку, чтобы привязать или отвязать форму. "
            "У одной кнопки может быть только одна форма.",
            reply_markup=admin_form_bindings(form_id, buttons, bound_ids),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:form_bind_btn:"))
async def admin_form_bind_button(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        _, _, form_id_raw, button_id_raw = callback.data.split(":")
        form_id = int(form_id_raw)
        button_id = int(button_id_raw)
    except (AttributeError, ValueError):
        await callback.answer("Некорректные данные", show_alert=True)
        return
    form = await db.get_form(form_id)
    button = await db.get_button(button_id)
    if not form or not button:
        await callback.answer("Форма или кнопка не найдена", show_alert=True)
        return
    current = await db.get_bound_form(button_id)
    if current and int(current["id"]) == form_id:
        await db.set_button_form(button_id, None)
        notice = "Отвязано"
    else:
        await db.set_button_form(button_id, form_id)
        notice = "Привязано"
    buttons = await db.list_buttons()
    bound_ids = set(await db.list_form_button_ids(form_id))
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(
            reply_markup=admin_form_bindings(form_id, buttons, bound_ids)
        )
    await callback.answer(notice)


@router.callback_query(F.data == "adm:pricing")
async def admin_pricing_open(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    forms = await db.list_forms_with_pricing()
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    text = (
        "<b>💰 Тарифы и расчёт аренды</b>\n\n"
        "Выберите форму. Для форм с датой, временем начала и окончания бот может "
        "автоматически рассчитать предварительную стоимость аренды.\n\n"
        "Расчёт: базовая стоимость + каждый начатый дополнительный час + выбранные доп. услуги. "
        "Здесь же настраиваются технические буферы для монтажа и уборки."
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            text, reply_markup=admin_pricing_forms(forms, currency)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:pricing_form:"))
async def admin_pricing_form_open(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        form_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная форма", show_alert=True)
        return
    form = await db.get_form(form_id)
    if not form:
        await callback.answer("Форма не найдена", show_alert=True)
        return
    pricing = await db.get_form_pricing(form_id)
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            await _pricing_admin_text(form, pricing),
            reply_markup=admin_pricing_form(form_id, pricing, currency),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:pricing_toggle:"))
async def admin_pricing_toggle(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        form_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная форма", show_alert=True)
        return
    form = await db.get_form(form_id)
    if not form:
        await callback.answer("Форма не найдена", show_alert=True)
        return
    pricing = await db.get_form_pricing(form_id)
    new_enabled = not bool(pricing.get("enabled"))
    if new_enabled:
        questions = await db.list_form_questions(form_id)
        if not _form_supports_time_pricing(questions):
            await callback.answer(
                "Для авторасчёта нужны дата, время начала и окончания.", show_alert=True
            )
            return
        if int(pricing.get("base_amount") or 0) <= 0 and int(pricing.get("extra_hour_amount") or 0) <= 0:
            await callback.answer(
                "Сначала задайте базовую стоимость или цену дополнительного часа.",
                show_alert=True,
            )
            return
    await db.update_form_pricing(form_id, "enabled", int(new_enabled))
    pricing = await db.get_form_pricing(form_id)
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            await _pricing_admin_text(form, pricing),
            reply_markup=admin_pricing_form(form_id, pricing, currency),
        )
    await callback.answer("Расчёт включён" if new_enabled else "Расчёт выключен")


async def _pricing_edit_start(
    callback: CallbackQuery, state: FSMContext, *, field: str
) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        form_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная форма", show_alert=True)
        return
    form = await db.get_form(form_id)
    if not form:
        await callback.answer("Форма не найдена", show_alert=True)
        return
    pricing = await db.get_form_pricing(form_id)
    await state.clear()
    await state.update_data(pricing_form_id=form_id)
    if field == "base_amount":
        await state.set_state(AdminStates.pricing_base_amount)
        prompt = (
            "Введите базовую стоимость аренды целым числом.\n"
            "Например: <code>60000</code> или <code>60 000</code>.\n\n"
            f"Сейчас: {_money_text(pricing.get('base_amount'), str(await db.get_setting('crm_currency', '₽') or '₽'))}"
        )
    elif field == "included_hours":
        await state.set_state(AdminStates.pricing_included_hours)
        prompt = (
            "Сколько полных часов включает базовая стоимость?\n"
            "Например: <code>6</code>. Если нужен чисто почасовой тариф — отправьте <code>0</code>.\n\n"
            f"Сейчас: {int(pricing.get('included_hours') or 0)} ч"
        )
    elif field == "buffer_before_minutes":
        await state.set_state(AdminStates.pricing_buffer_before)
        prompt = (
            "Введите технический буфер <b>до</b> аренды. Он блокирует календарь для монтажа, "
            "но не добавляется к оплачиваемой длительности.\n\n"
            "Можно ввести минуты (<code>120</code>), часы (<code>2ч</code>) или <code>1:30</code>. "
            "Для отключения — <code>0</code>.\n\n"
            f"Сейчас: {_duration_minutes_text(pricing.get('buffer_before_minutes'))}"
        )
    elif field == "buffer_after_minutes":
        await state.set_state(AdminStates.pricing_buffer_after)
        prompt = (
            "Введите технический буфер <b>после</b> аренды. Он блокирует календарь для уборки/демонтажа, "
            "но не увеличивает цену аренды.\n\n"
            "Можно ввести минуты (<code>120</code>), часы (<code>2ч</code>) или <code>1:30</code>. "
            "Для отключения — <code>0</code>.\n\n"
            f"Сейчас: {_duration_minutes_text(pricing.get('buffer_after_minutes'))}"
        )
    else:
        await state.set_state(AdminStates.pricing_extra_hour_amount)
        prompt = (
            "Введите стоимость каждого <b>начатого</b> дополнительного часа.\n"
            "Например: <code>10000</code>. Можно указать <code>0</code>, чтобы отключить доплату.\n\n"
            f"Сейчас: {_money_text(pricing.get('extra_hour_amount'), str(await db.get_setting('crm_currency', '₽') or '₽'))}"
        )
    if isinstance(callback.message, Message):
        await callback.message.answer(prompt)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:pricing_base:"))
async def admin_pricing_base_start(callback: CallbackQuery, state: FSMContext) -> None:
    await _pricing_edit_start(callback, state, field="base_amount")


@router.callback_query(F.data.startswith("adm:pricing_hours:"))
async def admin_pricing_hours_start(callback: CallbackQuery, state: FSMContext) -> None:
    await _pricing_edit_start(callback, state, field="included_hours")


@router.callback_query(F.data.startswith("adm:pricing_extra:"))
async def admin_pricing_extra_start(callback: CallbackQuery, state: FSMContext) -> None:
    await _pricing_edit_start(callback, state, field="extra_hour_amount")


@router.callback_query(F.data.startswith("adm:pricing_buffer_before:"))
async def admin_pricing_buffer_before_start(callback: CallbackQuery, state: FSMContext) -> None:
    await _pricing_edit_start(callback, state, field="buffer_before_minutes")


@router.callback_query(F.data.startswith("adm:pricing_buffer_after:"))
async def admin_pricing_buffer_after_start(callback: CallbackQuery, state: FSMContext) -> None:
    await _pricing_edit_start(callback, state, field="buffer_after_minutes")


async def _pricing_save_value(
    message: Message, state: FSMContext, *, field: str
) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    data = await state.get_data()
    form_id = int(data.get("pricing_form_id") or 0)
    form = await db.get_form(form_id)
    if not form:
        await state.clear()
        await message.answer("Форма не найдена.")
        return
    if field == "included_hours":
        raw = (message.text or "").strip()
        if not re.fullmatch(r"\d{1,2}", raw):
            await message.answer("Введите количество часов целым числом, например 6 или 0.")
            return
        value = int(raw)
        if value < 0 or value > 24:
            await message.answer("Допустимое значение: от 0 до 24 часов.")
            return
    elif field in {"buffer_before_minutes", "buffer_after_minutes"}:
        parsed_buffer = _parse_buffer_minutes(message.text)
        if parsed_buffer is None:
            await message.answer("Введите 0–1440 минут, например 120, 2ч или 1:30.")
            return
        value = parsed_buffer
    else:
        parsed = _parse_money_input(message.text)
        if parsed is None:
            await message.answer("Введите сумму целым числом, например 60000.")
            return
        value = parsed
    await db.update_form_pricing(form_id, field, value)
    await state.clear()
    pricing = await db.get_form_pricing(form_id)
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    await message.answer(
        await _pricing_admin_text(form, pricing),
        reply_markup=admin_pricing_form(form_id, pricing, currency),
    )


@router.message(AdminStates.pricing_base_amount)
async def admin_pricing_base_save(message: Message, state: FSMContext) -> None:
    await _pricing_save_value(message, state, field="base_amount")


@router.message(AdminStates.pricing_included_hours)
async def admin_pricing_hours_save(message: Message, state: FSMContext) -> None:
    await _pricing_save_value(message, state, field="included_hours")


@router.message(AdminStates.pricing_extra_hour_amount)
async def admin_pricing_extra_save(message: Message, state: FSMContext) -> None:
    await _pricing_save_value(message, state, field="extra_hour_amount")


@router.message(AdminStates.pricing_buffer_before)
async def admin_pricing_buffer_before_save(message: Message, state: FSMContext) -> None:
    await _pricing_save_value(message, state, field="buffer_before_minutes")


@router.message(AdminStates.pricing_buffer_after)
async def admin_pricing_buffer_after_save(message: Message, state: FSMContext) -> None:
    await _pricing_save_value(message, state, field="buffer_after_minutes")


async def _render_admin_addons(message: Message, form_id: int) -> None:
    form = await db.get_form(form_id)
    if not form:
        await message.answer("Форма не найдена.")
        return
    addons = await db.list_form_addons(form_id)
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    active = sum(1 for item in addons if item.get("enabled"))
    text = (
        f"<b>🧰 Доп. услуги · {html.escape(str(form['name']))}</b>\n\n"
        f"Активно: {active} · всего: {len(addons)}\n\n"
        "Клиент сможет отметить несколько услуг перед подтверждением заявки. "
        "Стоимость выбранных услуг автоматически прибавится к расчёту аренды."
    )
    await message.edit_text(text, reply_markup=admin_addons_list(form_id, addons, currency))


@router.callback_query(F.data.startswith("adm:addons:"))
async def admin_addons_open(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        form_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная форма", show_alert=True)
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await _render_admin_addons(callback.message, form_id)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:addon_add:"))
async def admin_addon_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        form_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная форма", show_alert=True)
        return
    if not await db.get_form(form_id):
        await callback.answer("Форма не найдена", show_alert=True)
        return
    await state.clear()
    await state.update_data(addon_form_id=form_id)
    await state.set_state(AdminStates.addon_add_name)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Введите название дополнительной услуги. Например: <code>Звук</code>, <code>Свет</code> или <code>Уборка</code>."
        )
    await callback.answer()


@router.message(AdminStates.addon_add_name)
async def admin_addon_add_name_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    name = (message.text or "").strip()
    if len(name) < 2 or len(name) > 80:
        await message.answer("Название должно быть от 2 до 80 символов.")
        return
    await state.update_data(addon_name=name)
    await state.set_state(AdminStates.addon_add_amount)
    await message.answer("Введите стоимость услуги, например <code>15000</code>. Для бесплатной услуги — <code>0</code>.")


@router.message(AdminStates.addon_add_amount)
async def admin_addon_add_amount_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    amount = _parse_money_input(message.text)
    if amount is None:
        await message.answer("Введите сумму целым числом, например 15000 или 0.")
        return
    data = await state.get_data()
    form_id = int(data.get("addon_form_id") or 0)
    name = str(data.get("addon_name") or "").strip()
    if not form_id or not name:
        await state.clear()
        await message.answer("Сессия настройки устарела. Откройте тариф заново.")
        return
    await db.add_form_addon(form_id, name, amount)
    await state.clear()
    addons = await db.list_form_addons(form_id)
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    await message.answer(
        f"✅ Услуга «{html.escape(name)}» добавлена.",
        reply_markup=admin_addons_list(form_id, addons, currency),
    )


def _addon_admin_text(addon: dict, currency: str) -> str:
    return (
        f"<b>🧰 {html.escape(str(addon['name']))}</b>\n\n"
        f"Цена: <b>{html.escape(_money_text(addon.get('amount'), currency))}</b>\n"
        f"Статус: {'🟢 включена' if addon.get('enabled') else '⚪ выключена'}"
    )


@router.callback_query(F.data.startswith("adm:addon:"))
async def admin_addon_open(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        addon_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная услуга", show_alert=True)
        return
    addon = await db.get_form_addon(addon_id)
    if not addon:
        await callback.answer("Услуга не найдена", show_alert=True)
        return
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            _addon_admin_text(addon, currency), reply_markup=admin_addon_edit(addon)
        )
    await callback.answer()


async def _addon_edit_start(callback: CallbackQuery, state: FSMContext, field: str) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        addon_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная услуга", show_alert=True)
        return
    addon = await db.get_form_addon(addon_id)
    if not addon:
        await callback.answer("Услуга не найдена", show_alert=True)
        return
    await state.clear()
    await state.update_data(addon_id=addon_id)
    if field == "name":
        await state.set_state(AdminStates.addon_edit_name)
        prompt = f"Введите новое название. Сейчас: <b>{html.escape(str(addon['name']))}</b>"
    else:
        await state.set_state(AdminStates.addon_edit_amount)
        prompt = f"Введите новую стоимость. Сейчас: <b>{_money_text(addon.get('amount'), str(await db.get_setting('crm_currency', '₽') or '₽'))}</b>"
    if isinstance(callback.message, Message):
        await callback.message.answer(prompt)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:addon_name:"))
async def admin_addon_name_start(callback: CallbackQuery, state: FSMContext) -> None:
    await _addon_edit_start(callback, state, "name")


@router.callback_query(F.data.startswith("adm:addon_amount:"))
async def admin_addon_amount_start(callback: CallbackQuery, state: FSMContext) -> None:
    await _addon_edit_start(callback, state, "amount")


@router.message(AdminStates.addon_edit_name)
async def admin_addon_name_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    name = (message.text or "").strip()
    if len(name) < 2 or len(name) > 80:
        await message.answer("Название должно быть от 2 до 80 символов.")
        return
    data = await state.get_data()
    addon_id = int(data.get("addon_id") or 0)
    await db.update_form_addon_field(addon_id, "name", name)
    addon = await db.get_form_addon(addon_id)
    await state.clear()
    if addon:
        currency = str(await db.get_setting("crm_currency", "₽") or "₽")
        await message.answer(_addon_admin_text(addon, currency), reply_markup=admin_addon_edit(addon))


@router.message(AdminStates.addon_edit_amount)
async def admin_addon_amount_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    amount = _parse_money_input(message.text)
    if amount is None:
        await message.answer("Введите сумму целым числом, например 15000 или 0.")
        return
    data = await state.get_data()
    addon_id = int(data.get("addon_id") or 0)
    await db.update_form_addon_field(addon_id, "amount", amount)
    addon = await db.get_form_addon(addon_id)
    await state.clear()
    if addon:
        currency = str(await db.get_setting("crm_currency", "₽") or "₽")
        await message.answer(_addon_admin_text(addon, currency), reply_markup=admin_addon_edit(addon))


@router.callback_query(F.data.startswith("adm:addon_toggle:"))
async def admin_addon_toggle(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    addon_id = int((callback.data or "").rsplit(":", 1)[1])
    addon = await db.get_form_addon(addon_id)
    if not addon:
        await callback.answer("Услуга не найдена", show_alert=True)
        return
    await db.update_form_addon_field(addon_id, "enabled", 0 if addon.get("enabled") else 1)
    addon = await db.get_form_addon(addon_id)
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    if isinstance(callback.message, Message) and addon:
        await callback.message.edit_text(_addon_admin_text(addon, currency), reply_markup=admin_addon_edit(addon))
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("adm:addon_delete:"))
async def admin_addon_delete(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    addon_id = int((callback.data or "").rsplit(":", 1)[1])
    addon = await db.get_form_addon(addon_id)
    if not addon:
        await callback.answer("Услуга уже удалена", show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"Удалить услугу <b>{html.escape(str(addon['name']))}</b>?",
            reply_markup=admin_addon_delete_confirm(addon),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:addon_delete_yes:"))
async def admin_addon_delete_yes(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    addon_id = int((callback.data or "").rsplit(":", 1)[1])
    addon = await db.get_form_addon(addon_id)
    if not addon:
        await callback.answer("Услуга уже удалена", show_alert=True)
        return
    form_id = int(addon["form_id"])
    await db.delete_form_addon(addon_id)
    addons = await db.list_form_addons(form_id)
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Услуга удалена.", reply_markup=admin_addons_list(form_id, addons, currency)
        )
    await callback.answer("Удалено")


@router.callback_query(F.data.startswith("adm:reqs:"))
async def admin_requests(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    status_filter = (callback.data or "adm:reqs:all").rsplit(":", 1)[1]
    allowed = {"all", "new", "in_progress", "confirmed", "paid", "completed", "cancelled"}
    if status_filter not in allowed:
        status_filter = "all"
    items = await db.list_submissions(None if status_filter == "all" else status_filter, limit=30)
    counts = await db.submission_status_counts()
    text = (
        "<b>📋 Заявки</b>\n\n"
        f"🆕 Новые: {counts['new']} · 🟡 В работе: {counts['in_progress']}\n"
        f"✅ Подтверждены: {counts['confirmed']} · 💰 Оплачены: {counts['paid']}\n"
        f"🏁 Завершены: {counts['completed']} · ❌ Отказ: {counts['cancelled']}\n\n"
        "Нажмите заявку, чтобы открыть карточку и изменить статус."
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=admin_submissions_list(_localize_submission_dates(items), status_filter))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:req:"))
async def admin_request_open(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        submission_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная заявка", show_alert=True)
        return
    submission = await db.get_submission(submission_id)
    if not submission:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            await _submission_admin_text(submission),
            reply_markup=admin_submission_card(submission),
        )
    await callback.answer()



async def _apply_submission_status_change(
    bot: Bot,
    submission_id: int,
    new_status: str,
    admin_user_id: int | None = None,
) -> tuple[bool, str]:
    """Change request status from Telegram or Web Admin with identical booking logic."""
    if new_status not in SUBMISSION_STATUS_NAMES:
        return False, "Неизвестный статус"
    submission = await db.get_submission(submission_id)
    if not submission:
        return False, "Заявка не найдена"
    old_status = str(submission.get("status") or "new")
    if old_status == new_status:
        return True, f"Статус уже: {SUBMISSION_STATUS_NAMES[new_status]}"

    questions = (
        await db.list_form_questions(int(submission["form_id"]))
        if submission.get("form_id") else []
    )
    interval = _booking_interval(questions, submission.get("answers") or {}) if questions else None
    if new_status in BOOKING_STATUSES and interval:
        if await _booking_interval_conflicts_for_form(
            int(submission["form_id"]), interval, exclude_submission_id=submission_id
        ):
            return False, "Интервал пересекается с занятой бронью"

    await db.update_submission_status(submission_id, new_status, admin_user_id)
    if new_status in BOOKING_STATUSES and interval:
        pricing = await db.get_form_pricing(int(submission["form_id"]))
        buffered = _booking_interval_with_buffers(
            interval,
            int(pricing.get("buffer_before_minutes") or 0),
            int(pricing.get("buffer_after_minutes") or 0),
        )
        segments = (buffered or {}).get("technical_segments") or interval["segments"]
        before = int(pricing.get("buffer_before_minutes") or 0)
        after = int(pricing.get("buffer_after_minutes") or 0)
        buffer_note = ""
        if before or after:
            buffer_note = f" · буфер -{_duration_minutes_text(before)} / +{_duration_minutes_text(after)}"
        await db.replace_submission_availability(
            submission_id,
            segments,
            note=f"Заявка №{submission_id}: {submission['form_name']}{buffer_note}",
        )
    else:
        await db.delete_submission_availability(submission_id)

    updated = await db.get_submission(submission_id)
    if not updated:
        return True, f"{SUBMISSION_STATUS_NAMES[new_status]}"
    notified, error = await _notify_submission_status_change(bot, updated, new_status)
    if notified:
        return True, f"{SUBMISSION_STATUS_NAMES[new_status]} · клиент уведомлён"
    return True, f"Статус сохранён, но клиент не уведомлён: {(error or 'неизвестная ошибка')[:160]}"


async def _apply_submission_amount_change(
    bot: Bot,
    submission_id: int,
    new_amount: int,
    admin_user_id: int | None = None,
) -> tuple[bool, str]:
    """Update total amount and notify the customer. The DB update is authoritative."""
    submission = await db.get_submission(submission_id)
    if not submission:
        return False, "Заявка не найдена"
    new_amount = max(0, int(new_amount))
    prepayment = max(0, int(submission.get("prepayment_amount") or 0))
    if new_amount and prepayment > new_amount:
        return False, "Стоимость не может быть меньше предоплаты"
    old_amount = max(0, int(submission.get("total_amount") or 0))
    if old_amount == new_amount:
        return True, "Стоимость не изменилась"
    await db.update_submission_crm_field(
        submission_id, "total_amount", new_amount, admin_user_id
    )
    updated = await db.get_submission(submission_id)
    if not updated:
        return True, "Стоимость сохранена"
    notified, error = await _notify_submission_amount_change(
        bot, updated, old_amount, new_amount
    )
    if notified:
        return True, "Стоимость обновлена · клиент уведомлён"
    return True, f"Стоимость обновлена, но клиент не уведомлён: {(error or 'неизвестная ошибка')[:160]}"


@router.callback_query(F.data.startswith("adm:req_status:"))
async def admin_request_status(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        _, _, submission_raw, new_status = (callback.data or "").split(":", 3)
        submission_id = int(submission_raw)
    except (ValueError, TypeError):
        await callback.answer("Некорректные данные", show_alert=True)
        return
    if new_status not in SUBMISSION_STATUS_NAMES:
        await callback.answer("Неизвестный статус", show_alert=True)
        return
    submission = await db.get_submission(submission_id)
    if not submission:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    old_status = str(submission.get("status") or "new")
    if old_status == new_status:
        await callback.answer(f"Статус уже: {SUBMISSION_STATUS_NAMES[new_status]}")
        return
    questions = await db.list_form_questions(int(submission["form_id"])) if submission.get("form_id") else []
    interval = _booking_interval(questions, submission.get("answers") or {}) if questions else None
    if new_status in BOOKING_STATUSES and interval:
        if await _booking_interval_conflicts_for_form(
            int(submission["form_id"]), interval, exclude_submission_id=submission_id
        ):
            await callback.answer(
                "Нельзя подтвердить: интервал пересекается с занятой бронью. Проверьте раздел «Занятость».",
                show_alert=True,
            )
            return
    await db.update_submission_status(submission_id, new_status, callback.from_user.id)
    if new_status in BOOKING_STATUSES and interval:
        pricing = await db.get_form_pricing(int(submission["form_id"]))
        buffered = _booking_interval_with_buffers(
            interval,
            int(pricing.get("buffer_before_minutes") or 0),
            int(pricing.get("buffer_after_minutes") or 0),
        )
        segments = (buffered or {}).get("technical_segments") or interval["segments"]
        before = int(pricing.get("buffer_before_minutes") or 0)
        after = int(pricing.get("buffer_after_minutes") or 0)
        buffer_note = ""
        if before or after:
            buffer_note = f" · буфер -{_duration_minutes_text(before)} / +{_duration_minutes_text(after)}"
        await db.replace_submission_availability(
            submission_id,
            segments,
            note=f"Заявка №{submission_id}: {submission['form_name']}{buffer_note}",
        )
    else:
        await db.delete_submission_availability(submission_id)
    updated_submission = await db.get_submission(submission_id)
    if isinstance(callback.message, Message) and updated_submission:
        await callback.message.edit_text(
            await _submission_admin_text(updated_submission),
            reply_markup=admin_submission_card(updated_submission),
        )

    notified = False
    notification_error: str | None = None
    if updated_submission:
        notified, notification_error = await _notify_submission_status_change(
            bot, updated_submission, new_status
        )

    if notified:
        await callback.answer(
            f"{SUBMISSION_STATUS_NAMES[new_status]} · клиент уведомлён"
        )
    else:
        reason = notification_error or "неизвестная ошибка"
        if len(reason) > 120:
            reason = reason[:117] + "..."
        await callback.answer(
            f"Статус сохранён, но клиент не уведомлён: {reason}",
            show_alert=True,
        )


def _parse_admin_money(text: str | None) -> int | None:
    raw = (text or "").strip().casefold().replace("₽", "").replace("руб.", "").replace("руб", "").strip()
    if not raw:
        return None
    if raw in {"0", "-", "нет"}:
        return 0
    if not re.fullmatch(r"\d[\d\s_]*", raw):
        return None
    try:
        value = int(re.sub(r"[\s_]", "", raw))
    except ValueError:
        return None
    return value if 0 <= value <= 1_000_000_000 else None


@router.callback_query(F.data == "adm:req_search")
async def admin_request_search_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.submission_search)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "<b>🔎 Поиск заявок</b>\n\n"
            "Введите номер заявки, имя, @username, телефон, название формы или текст из заявки."
        )
    await callback.answer()


@router.message(AdminStates.submission_search)
async def admin_request_search_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    query = (message.text or "").strip()
    if len(query) < 2 and not query.isdigit():
        await message.answer("Введите хотя бы 2 символа или номер заявки.")
        return
    items = await db.search_submissions(query, limit=30)
    await state.clear()
    text = (
        "<b>🔎 Результаты поиска</b>\n\n"
        f"Запрос: <code>{html.escape(query)}</code>\n"
        f"Найдено: {len(items)}"
    )
    await message.answer(text, reply_markup=admin_submissions_list(_localize_submission_dates(items), "all"))


async def _start_submission_field_edit(callback: CallbackQuery, state: FSMContext, field: str) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        sid = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная заявка", show_alert=True)
        return
    submission = await db.get_submission(sid)
    if not submission:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    await state.clear()
    await state.update_data(submission_id=sid)
    if field == "amount":
        await state.set_state(AdminStates.submission_amount)
        prompt = "💵 Введите полную стоимость заявки. Например: 90000\n\n0 — очистить сумму."
    elif field == "prepayment":
        await state.set_state(AdminStates.submission_prepayment)
        prompt = "💳 Введите сумму предоплаты / уже полученной оплаты. Например: 30000\n\n0 — очистить."
    else:
        await state.set_state(AdminStates.submission_note)
        prompt = "🗒 Введите внутреннюю заметку. Клиент её не увидит.\n\nОтправьте - чтобы очистить заметку."
    if isinstance(callback.message, Message):
        await callback.message.answer(prompt)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:req_amount:"))
async def admin_request_amount_start(callback: CallbackQuery, state: FSMContext) -> None:
    await _start_submission_field_edit(callback, state, "amount")


@router.callback_query(F.data.startswith("adm:req_prepayment:"))
async def admin_request_prepayment_start(callback: CallbackQuery, state: FSMContext) -> None:
    await _start_submission_field_edit(callback, state, "prepayment")


@router.callback_query(F.data.startswith("adm:req_note:"))
async def admin_request_note_start(callback: CallbackQuery, state: FSMContext) -> None:
    await _start_submission_field_edit(callback, state, "note")


async def _finish_submission_field_edit(message: Message, state: FSMContext, bot: Bot, field: str) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    data = await state.get_data()
    sid = int(data.get("submission_id") or 0)
    submission = await db.get_submission(sid)
    if not submission:
        await state.clear()
        await message.answer("Заявка не найдена.")
        return
    if field in {"total_amount", "prepayment_amount"}:
        value = _parse_admin_money(message.text)
        if value is None:
            await message.answer("Введите сумму цифрами, например 90000 или 90 000. Для очистки — 0.")
            return
        if field == "prepayment_amount" and int(submission.get("total_amount") or 0) and value > int(submission.get("total_amount") or 0):
            await message.answer("Предоплата не может быть больше полной стоимости заявки.")
            return
        if field == "total_amount" and value and int(submission.get("prepayment_amount") or 0) > value:
            await message.answer("Стоимость не может быть меньше уже указанной предоплаты.")
            return
    else:
        raw = (message.text or "").strip()
        value = "" if raw == "-" else raw
        if len(value) > 1500:
            await message.answer("Заметка слишком длинная. Максимум 1500 символов.")
            return
    old_amount = int(submission.get("total_amount") or 0)
    await db.update_submission_crm_field(
        sid,
        field,
        value,
        message.from_user.id if message.from_user else None,
    )
    await state.clear()
    updated = await db.get_submission(sid)
    if updated:
        await message.answer(
            await _submission_admin_text(updated),
            reply_markup=admin_submission_card(updated),
        )
        if field == "total_amount" and old_amount != int(updated.get("total_amount") or 0):
            notified, error = await _notify_submission_amount_change(
                bot, updated, old_amount, int(updated.get("total_amount") or 0)
            )
            if notified:
                await message.answer("✅ Стоимость обновлена. Клиент уведомлён в исходном чате.")
            else:
                reason = (error or "неизвестная ошибка")[:180]
                await message.answer(
                    f"⚠️ Стоимость обновлена, но клиент не уведомлён: {reason}"
                )
    else:
        await message.answer("✅ Карточка заявки обновлена.")


@router.message(AdminStates.submission_amount)
async def admin_request_amount_save(message: Message, state: FSMContext, bot: Bot) -> None:
    await _finish_submission_field_edit(message, state, bot, "total_amount")


@router.message(AdminStates.submission_prepayment)
async def admin_request_prepayment_save(message: Message, state: FSMContext, bot: Bot) -> None:
    await _finish_submission_field_edit(message, state, bot, "prepayment_amount")


@router.message(AdminStates.submission_note)
async def admin_request_note_save(message: Message, state: FSMContext, bot: Bot) -> None:
    await _finish_submission_field_edit(message, state, bot, "internal_note")


@router.callback_query(F.data.startswith("adm:req_history:"))
async def admin_request_history(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        sid = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная заявка", show_alert=True)
        return
    submission = await db.get_submission(sid)
    if not submission:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    history = await db.list_client_submissions(
        user_id=submission.get("user_id"),
        chat_id=submission.get("chat_id"),
        limit=10,
    )
    events = await db.list_submission_events(sid, limit=8)
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    lines = ["<b>👤 История клиента</b>", ""]
    for item in history:
        status = SUBMISSION_STATUS_NAMES.get(str(item.get("status") or "new"), "•")
        amount = _money_text(item.get("total_amount"), currency)
        lines.append(
            f"{status} · №{item['id']} · {html.escape(str(item['form_name']))} · {html.escape(amount)}"
        )
    if not history:
        lines.append("Других заявок пока нет.")
    lines.extend(["", "<b>Последние изменения этой заявки</b>"])
    event_names = {
        "created": "Создана",
        "status": "Статус",
        "total_amount": "Стоимость",
        "prepayment_amount": "Предоплата",
        "internal_note": "Заметка",
    }
    for event in events:
        event_type = str(event.get("event_type"))
        name = event_names.get(event_type, event_type)
        event_value = str(event.get("new_value") or "—")
        if event_type == "status":
            event_value = SUBMISSION_STATUS_NAMES.get(event_value, event_value)
        lines.append(
            f"• {html.escape(name)}: {html.escape(event_value)} · "
            f"{html.escape(_format_local_timestamp(event.get('created_at')))}"
        )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=admin_submission_card(submission),
        )
    await callback.answer()


@router.callback_query(F.data == "adm:status_templates")
async def admin_status_templates_open(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "<b>💬 Шаблоны статусов</b>\n\n"
            "Тексты отправляются клиенту при смене статуса. Выберите статус для просмотра или редактирования.",
            reply_markup=admin_status_templates(),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:status_tpl:"))
async def admin_status_template_open(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    status = (callback.data or "").rsplit(":", 1)[1]
    if status not in SUBMISSION_STATUS_NAMES:
        await callback.answer("Неизвестный статус", show_alert=True)
        return
    template = await db.get_setting(
        f"status_template_{status}", DEFAULT_STATUS_TEMPLATES[status]
    )
    text = (
        f"<b>{SUBMISSION_STATUS_NAMES[status]}</b>\n\n"
        f"<pre>{html.escape(str(template or ''))}</pre>\n\n"
        "Переменные: <code>{id}</code>, <code>{form}</code>, <code>{status}</code>, "
        "<code>{client}</code>, <code>{amount}</code>, <code>{prepayment}</code>, <code>{balance}</code>."
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            text,
            reply_markup=admin_status_template_edit(status),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:status_tpl_edit:"))
async def admin_status_template_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    status = (callback.data or "").rsplit(":", 1)[1]
    if status not in SUBMISSION_STATUS_NAMES:
        await callback.answer("Неизвестный статус", show_alert=True)
        return
    await state.clear()
    await state.update_data(status_template_key=status)
    await state.set_state(AdminStates.status_template)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"Отправьте новый шаблон для статуса {SUBMISSION_STATUS_NAMES[status]}.\n\n"
            "Можно использовать {id}, {form}, {status}, {client}, {amount}, {prepayment}, {balance}."
        )
    await callback.answer()


@router.message(AdminStates.status_template)
async def admin_status_template_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    data = await state.get_data()
    status = str(data.get("status_template_key") or "")
    if status not in SUBMISSION_STATUS_NAMES:
        await state.clear()
        return
    value = message.text or ""
    if not value.strip() or len(value) > 3500:
        await message.answer("Шаблон должен содержать текст и быть короче 3500 символов.")
        return
    await db.set_setting(f"status_template_{status}", value)
    await state.clear()
    await message.answer(
        "✅ Шаблон сохранён.",
        reply_markup=admin_status_template_edit(status),
    )


@router.callback_query(F.data.startswith("adm:status_tpl_default:"))
async def admin_status_template_default(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    status = (callback.data or "").rsplit(":", 1)[1]
    if status not in DEFAULT_STATUS_TEMPLATES:
        await callback.answer("Неизвестный статус", show_alert=True)
        return
    await db.set_setting(f"status_template_{status}", DEFAULT_STATUS_TEMPLATES[status])
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"<b>{SUBMISSION_STATUS_NAMES[status]}</b>\n\n"
            f"<pre>{html.escape(DEFAULT_STATUS_TEMPLATES[status])}</pre>",
            reply_markup=admin_status_template_edit(status),
        )
    await callback.answer("Шаблон восстановлен")


@router.callback_query(F.data == "adm:availability")
async def admin_availability_open(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    today_iso = local_today().isoformat()
    blocks = await db.list_availability_blocks(start_date=today_iso, limit=30)
    text = (
        "<b>📅 Занятость</b>\n\n"
        "× в клиентском календаре означает полностью занятую дату, "
        "• — на дате есть занятые интервалы.\n\n"
        "Подтверждённые/оплаченные заявки блокируют дату автоматически. "
        "Также можно добавить блокировку вручную."
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=admin_availability(blocks))
    await callback.answer()


@router.callback_query(F.data == "adm:availability_add")
async def admin_availability_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.availability_date)
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите дату блокировки в формате ДД.ММ.ГГГГ:")
    await callback.answer()


@router.message(AdminStates.availability_date)
async def admin_availability_date_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    normalized = _parse_date_answer(message.text)
    parsed = _date_from_answer(normalized)
    if not normalized or not parsed:
        await message.answer("Не удалось распознать дату. Пример: 21.10.2026")
        return
    await state.update_data(availability_date=parsed.isoformat())
    await state.set_state(AdminStates.availability_period)
    await message.answer(
        "Теперь укажите период:\n\n"
        "• <code>весь день</code>\n"
        "• <code>18:00-23:00</code>\n"
        "• через полночь: <code>21:00-05:00</code>\n"
        "• можно добавить заметку: <code>18:00-23:00 | монтаж</code>"
    )


@router.message(AdminStates.availability_period)
async def admin_availability_period_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    raw = (message.text or "").strip()
    period_raw, _, note_raw = raw.partition("|")
    period = period_raw.strip().casefold()
    note = note_raw.strip() or None
    start_time = end_time = None
    overnight = False
    if period not in {"весь день", "весьдень", "all", "day"}:
        match = re.fullmatch(r"\s*(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})\s*", period_raw)
        if not match:
            await message.answer("Введите «весь день» или интервал, например 18:00-23:00 или 21:00-05:00.")
            return
        start_time = _parse_time_answer(match.group(1))
        end_time = _parse_time_answer(match.group(2))
        if not start_time or not end_time or end_time == start_time:
            await message.answer("Укажите разные корректные времена начала и окончания.")
            return
        overnight = _time_to_minutes(end_time) < _time_to_minutes(start_time)
    data = await state.get_data()
    date_iso = str(data["availability_date"])
    if start_time and end_time and overnight:
        next_date = (datetime.strptime(date_iso, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
        await db.add_availability_block(date_iso, start_time, "24:00", note=note)
        await db.add_availability_block(next_date, "00:00", end_time, note=note)
    else:
        await db.add_availability_block(date_iso, start_time, end_time, note=note)
    await state.clear()
    await message.answer("✅ Блокировка добавлена.")
    blocks = await db.list_availability_blocks(start_date=local_today().isoformat(), limit=30)
    await message.answer("📅 Занятость:", reply_markup=admin_availability(blocks))


@router.callback_query(F.data.startswith("adm:availability_del:"))
async def admin_availability_delete(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        block_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная блокировка", show_alert=True)
        return
    block = await db.get_availability_block(block_id)
    if block and block.get("source_submission_id"):
        await db.delete_submission_availability(int(block["source_submission_id"]))
    else:
        await db.delete_availability_block(block_id)
    blocks = await db.list_availability_blocks(start_date=local_today().isoformat(), limit=30)
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=admin_availability(blocks))
    await callback.answer("Удалено")


@router.callback_query(F.data == "adm:stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    stat = await db.stats()
    counts = await db.submission_status_counts()
    connection = await db.latest_business_connection()
    conn = "подключён" if connection and connection["enabled"] else "не подключён"
    text = (
        "<b>Статистика</b>\n\n"
        f"Уникальных чатов: {stat['contacts']}\n"
        f"Автоответов отправлено: {stat['auto_replies']}\n"
        f"Нажатий на кнопки: {stat['button_clicks']}\n"
        f"Заявок отправлено: {stat['submissions']}\n"
        f"Новых заявок: {counts['new']}\n"
        f"В работе: {counts['in_progress']}\n"
        f"Подтверждено / оплачено: {counts['confirmed'] + counts['paid']}\n"
        f"Business: {conn}"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=admin_main(await get_autoresponder_enabled()))
    await callback.answer()


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    await db.init()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    async def web_status_change(submission_id: int, new_status: str) -> tuple[bool, str]:
        return await _apply_submission_status_change(bot, submission_id, new_status, None)

    async def web_amount_change(submission_id: int, new_amount: int) -> tuple[bool, str]:
        return await _apply_submission_amount_change(bot, submission_id, new_amount, None)

    web_server = await start_web_admin(
        port=settings.status_port,
        db=db,
        timezone=APP_TIMEZONE,
        username=settings.web_admin_username,
        password=settings.web_admin_password,
        on_status_change=web_status_change,
        on_amount_change=web_amount_change,
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.set_my_commands(
        [
            BotCommand(command="admin", description="Открыть админ-панель"),
            BotCommand(command="start", description="Открыть меню"),
        ]
    )

    me = await bot.get_me()
    logger.info(
        "Бот @%s запущен. can_connect_to_business=%s",
        me.username,
        me.can_connect_to_business,
    )
    if not me.can_connect_to_business:
        logger.warning("Включите Business/Secretary Mode в @BotFather")

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        await web_server.close()
        await web_server.wait_closed()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
