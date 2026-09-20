from __future__ import annotations

import asyncio
import calendar
import html
import logging
import re
from datetime import datetime, timedelta, timezone

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
    admin_availability,
    admin_button_edit,
    admin_buttons_list,
    admin_form_bindings,
    admin_form_delete_confirm,
    admin_form_edit,
    admin_form_questions,
    admin_forms_list,
    admin_main,
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
    form_confirmation,
    form_question_nav,
    public_menu,
    time_slots_keyboard,
)
from .states import AdminStates
from .status import start_status_server

logger = logging.getLogger(__name__)

settings: Settings = load_settings()
db = Database(settings.database_path)
router = Router(name="main")


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in settings.admin_ids


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
}


def _question_input_type(question: dict) -> str:
    value = str(question.get("input_type") or "text")
    return value if value in QUESTION_TYPE_NAMES else "text"


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


async def _busy_time_slots(date_iso: str | None, slots: list[str]) -> set[str]:
    if not date_iso:
        return set()
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


async def _booking_interval_conflicts(
    interval: dict | None, *, exclude_submission_id: int | None = None
) -> bool:
    if not interval:
        return False
    for date_iso, start_time, end_time in interval["segments"]:
        if await db.booking_conflicts(
            date_iso, start_time, end_time, exclude_submission_id=exclude_submission_id
        ):
            return True
    return False


async def _booking_end_time_options(
    questions: list[dict], answers: dict[str, str]
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
            if await _booking_interval_conflicts(_booking_interval(questions, temp_answers)):
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


async def _submission_admin_text(submission: dict) -> str:
    full_name = " ".join(
        part for part in [submission.get("first_name"), submission.get("last_name")] if part
    ).strip() or "Без имени"
    status = SUBMISSION_STATUS_NAMES.get(str(submission.get("status") or "new"), "•")
    currency = str(await db.get_setting("crm_currency", "₽") or "₽")
    total_amount = max(0, int(submission.get("total_amount") or 0))
    prepayment = max(0, int(submission.get("prepayment_amount") or 0))
    balance = max(0, total_amount - prepayment) if total_amount else 0
    lines = [
        f"<b>Заявка №{submission['id']}</b>",
        f"Статус: {status}",
        f"Форма: <b>{html.escape(str(submission['form_name']))}</b>",
        "",
        f"💵 Стоимость: <b>{html.escape(_money_text(total_amount, currency))}</b>",
        f"💳 Предоплата: <b>{html.escape(_money_text(prepayment, currency))}</b>",
        f"🧾 Остаток: <b>{html.escape(_money_text(balance, currency))}</b>",
        f"🗒 Заметка: {html.escape(str(submission.get('internal_note') or '—'))}",
        "",
        f"Клиент: {html.escape(full_name)}",
        f"Telegram: @{html.escape(str(submission['username']))}" if submission.get("username") else "Telegram: —",
        f"User ID: {submission.get('user_id') or '—'}",
        "",
    ]
    questions = await db.list_form_questions(int(submission["form_id"])) if submission.get("form_id") else []
    answers = submission.get("answers") or {}
    if questions:
        for question in questions:
            value = answers.get(str(question["id"])) or "—"
            lines.append(f"<b>{html.escape(str(question['label']))}:</b> {html.escape(str(value))}")
        interval_summary = _booking_interval_summary(_booking_interval(questions, answers))
        if interval_summary:
            lines.extend(["", f"<b>🕐 Интервал:</b> {html.escape(interval_summary)}"])
    else:
        for key, value in answers.items():
            lines.append(f"<b>Поле {html.escape(str(key))}:</b> {html.escape(str(value))}")
    lines.extend(["", f"Создана: {html.escape(str(submission.get('created_at') or '—'))}"])
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


def _form_preview_text(form: dict, questions: list[dict], answers: dict[str, str]) -> str:
    lines = ["✅ Проверьте заявку", "", str(form["name"]), ""]
    for question in questions:
        answer = answers.get(str(question["id"])) or "—"
        lines.append(f"{question['label']}: {answer}")
    interval_summary = _booking_interval_summary(_booking_interval(questions, answers))
    if interval_summary:
        lines.extend(["", f"🕐 Интервал: {interval_summary}"])
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
        await db.update_form_session(
            chat_id, current_index=len(questions), status="confirm", keyboard_question_id=0
        )
        session = await db.get_form_session(chat_id)
        if not session:
            return
        await _upsert_form_message(
            bot,
            session,
            text=_form_preview_text(form, questions, session["answers"]),
            reply_markup=form_confirmation(),
        )
        return

    question = questions[index]
    input_type = _question_input_type(question)
    suffix = "\n\nЭтот вопрос необязательный — его можно пропустить." if not question["required"] else ""
    existing = session["answers"].get(str(question["id"]))
    if existing:
        suffix += f"\n\nТекущий ответ: {existing}"

    if input_type == "date":
        today = datetime.now().date()
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
            options, busy_values = await _booking_end_time_options(questions, session["answers"])
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
        busy_slots = await _busy_time_slots(date_iso, slots)
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
        await _upsert_form_message(
            bot,
            session,
            text="Заявка уже заполнена. Используйте кнопки «Отправить заявку», «Изменить ответы» или «Отмена».",
            reply_markup=form_confirmation(),
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
            if await _booking_interval_conflicts(interval):
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
                if await _booking_interval_conflicts(interval):
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
    submission_id: int, form: dict, questions: list[dict], answers: dict[str, str]
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
        f"Business-соединение: {conn_text}\n"
        f"Чистая форма: {clean_form_text}\n\n"
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
    today_date = datetime.now().date()
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
        if await _booking_interval_conflicts(interval):
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
            if await _booking_interval_conflicts(interval):
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


@router.callback_query(F.data == "form:back")
async def form_back(callback: CallbackQuery, bot: Bot) -> None:
    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return
    questions = await db.list_form_questions(int(session["form_id"]))
    if not questions:
        await callback.answer("В форме нет вопросов", show_alert=True)
        return
    if session["status"] == "confirm":
        target = len(questions) - 1
    else:
        target = int(session["current_index"]) - 1
    if target < 0:
        await callback.answer("Это первый вопрос", show_alert=True)
        return
    await db.update_form_session(
        callback.message.chat.id, current_index=target, status="active", keyboard_question_id=0
    )
    await send_current_form_question(bot, callback.message.chat.id)
    await callback.answer()


@router.callback_query(F.data == "form:edit")
async def form_edit_answers(callback: CallbackQuery, bot: Bot) -> None:
    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return
    await db.update_form_session(
        callback.message.chat.id, current_index=0, status="active", keyboard_question_id=0
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
    if booking_interval and await _booking_interval_conflicts(booking_interval):
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

    submission_id = await db.create_form_submission(session, str(form["name"]))
    await _upsert_form_message(
        bot,
        session,
        text=_submission_chat_text(
            submission_id, form, questions, session["answers"]
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
            "📱 Контакт — ручной ввод телефона с проверкой; Telegram-контакт тоже принимается",
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
    if input_type not in {"text", "date", "time", "contact"}:
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
        await callback.message.edit_text(text, reply_markup=admin_submissions_list(items, status_filter))
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
        if await _booking_interval_conflicts(
            interval, exclude_submission_id=submission_id
        ):
            await callback.answer(
                "Нельзя подтвердить: интервал пересекается с занятой бронью. Проверьте раздел «Занятость».",
                show_alert=True,
            )
            return
    await db.update_submission_status(submission_id, new_status, callback.from_user.id)
    if new_status in BOOKING_STATUSES and interval:
        await db.replace_submission_availability(
            submission_id,
            interval["segments"],
            note=f"Заявка №{submission_id}: {submission['form_name']}",
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
    await message.answer(text, reply_markup=admin_submissions_list(items, "all"))


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


async def _finish_submission_field_edit(message: Message, state: FSMContext, field: str) -> None:
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
    else:
        await message.answer("✅ Карточка заявки обновлена.")


@router.message(AdminStates.submission_amount)
async def admin_request_amount_save(message: Message, state: FSMContext) -> None:
    await _finish_submission_field_edit(message, state, "total_amount")


@router.message(AdminStates.submission_prepayment)
async def admin_request_prepayment_save(message: Message, state: FSMContext) -> None:
    await _finish_submission_field_edit(message, state, "prepayment_amount")


@router.message(AdminStates.submission_note)
async def admin_request_note_save(message: Message, state: FSMContext) -> None:
    await _finish_submission_field_edit(message, state, "internal_note")


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
            f"{html.escape(str(event.get('created_at') or '')[:16])}"
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
    today_iso = datetime.now().date().isoformat()
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
    blocks = await db.list_availability_blocks(start_date=datetime.now().date().isoformat(), limit=30)
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
    blocks = await db.list_availability_blocks(start_date=datetime.now().date().isoformat(), limit=30)
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
    status_server = await start_status_server(settings.status_port)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
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
        status_server.close()
        await status_server.wait_closed()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
