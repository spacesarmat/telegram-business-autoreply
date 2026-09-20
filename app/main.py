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
    admin_main,
    delete_confirm,
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


async def render_admin_home() -> tuple[str, object]:
    enabled = await get_autoresponder_enabled()
    cooldown = int(await db.get_setting("cooldown_hours", "168") or 168)
    columns = int(await db.get_setting("menu_columns", "1") or 1)
    connection = await db.latest_business_connection()

    if connection and connection["enabled"]:
        conn_text = "🟢 подключён"
        if not connection["can_reply"]:
            conn_text += " (нет права отвечать)"
    else:
        conn_text = "⚪ не подключён"

    text = (
        "<b>Автоответчик Telegram Business</b>\n\n"
        f"Автоответ: {'включён' if enabled else 'выключен'}\n"
        f"Повторный автоответ после паузы: {cooldown} ч.\n"
        f"Кнопок в строке: {columns}\n"
        f"Business-соединение: {conn_text}\n\n"
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
    await db.save_business_connection(
        connection_id=connection.id,
        owner_user_id=connection.user.id,
        user_chat_id=connection.user_chat_id,
        enabled=connection.is_enabled,
        can_reply=can_reply,
    )
    return await db.get_business_connection(connection_id)


@router.business_connection()
async def on_business_connection(event: BusinessConnection, bot: Bot) -> None:
    can_reply = bool(event.rights and event.rights.can_reply)
    await db.save_business_connection(
        connection_id=event.id,
        owner_user_id=event.user.id,
        user_chat_id=event.user_chat_id,
        enabled=event.is_enabled,
        can_reply=can_reply,
    )

    status = "подключён" if event.is_enabled else "отключён"
    rights = "есть право отвечать" if can_reply else "НЕТ права отвечать"
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(
                admin_id,
                f"Business-бот {status}. Статус: {rights}.",
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
    columns = int(await db.get_setting("menu_columns", "1") or 1)
    buttons = await db.list_buttons(enabled_only=True)

    try:
        await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=connection_id,
            text=greeting,
            parse_mode=None,
            reply_markup=public_menu(buttons, columns),
        )
        await db.mark_auto_reply(message.chat.id, now_iso)
    except TelegramAPIError:
        logger.exception("Не удалось отправить Business-автоответ в chat_id=%s", message.chat.id)


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
    status = "включена" if button["enabled"] else "выключена"
    text = (
        f"<b>{html.escape(button['title'])}</b>\n\n"
        f"Позиция: {button['position']}\n"
        f"Статус: {status}\n\n"
        f"<b>Ответ:</b>\n{html.escape(button['response'])}"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=admin_button_edit(button))
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
    if isinstance(callback.message, Message):
        status = "включена" if button["enabled"] else "выключена"
        text = (
            f"<b>{html.escape(button['title'])}</b>\n\n"
            f"Позиция: {button['position']}\n"
            f"Статус: {status}\n\n"
            f"<b>Ответ:</b>\n{html.escape(button['response'])}"
        )
        await callback.message.edit_text(text, reply_markup=admin_button_edit(button))
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
