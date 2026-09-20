from __future__ import annotations

import asyncio
import html
import logging
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
from .db import Database, utc_now_iso
from .keyboards import (
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
    delete_confirm,
    form_confirmation,
    form_question_nav,
    public_menu,
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


async def send_current_form_question(bot: Bot, chat_id: int) -> None:
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
        await db.update_form_session(chat_id, current_index=len(questions), status="confirm")
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
    suffix = "\n\nЭтот вопрос необязательный — его можно пропустить." if not question["required"] else ""
    existing = session["answers"].get(str(question["id"]))
    if existing:
        suffix += f"\n\nТекущий ответ: {existing}"
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

    answer = _message_answer_text(message)
    if not answer:
        await _upsert_form_message(
            bot,
            session,
            text="Пожалуйста, отправьте ответ текстом. Также можно отправить контакт или геолокацию.",
            reply_markup=form_question_nav(bool(questions[index]["required"]), index > 0),
        )
        return True

    if len(answer) > 1500:
        await _upsert_form_message(
            bot,
            session,
            text="Ответ слишком длинный. Пожалуйста, сократите его до 1500 символов.",
            reply_markup=form_question_nav(bool(questions[index]["required"]), index > 0),
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
        callback.message.chat.id, current_index=target, status="active"
    )
    await send_current_form_question(bot, callback.message.chat.id)
    await callback.answer()


@router.callback_query(F.data == "form:edit")
async def form_edit_answers(callback: CallbackQuery, bot: Bot) -> None:
    session = await _form_callback_session(callback)
    if not session or not isinstance(callback.message, Message):
        return
    await db.update_form_session(
        callback.message.chat.id, current_index=0, status="active"
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
    required = "да" if question["required"] else "нет"
    text = (
        f"<b>{html.escape(question['label'])}</b>\n\n"
        f"Позиция: {question['position']}\n"
        f"Обязательный: {required}\n\n"
        f"<b>Вопрос пользователю:</b>\n{html.escape(question['prompt'])}"
    )
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
    required = "да" if question["required"] else "нет"
    text = (
        f"<b>{html.escape(question['label'])}</b>\n\n"
        f"Позиция: {question['position']}\n"
        f"Обязательный: {required}\n\n"
        f"<b>Вопрос пользователю:</b>\n{html.escape(question['prompt'])}"
    )
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


@router.callback_query(F.data == "adm:stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    stat = await db.stats()
    connection = await db.latest_business_connection()
    conn = "подключён" if connection and connection["enabled"] else "не подключён"
    text = (
        "<b>Статистика</b>\n\n"
        f"Уникальных чатов: {stat['contacts']}\n"
        f"Автоответов отправлено: {stat['auto_replies']}\n"
        f"Нажатий на кнопки: {stat['button_clicks']}\n"
        f"Заявок отправлено: {stat['submissions']}\n"
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
