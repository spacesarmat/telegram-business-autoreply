from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def public_menu(buttons: list[dict], columns: int = 1) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    columns = max(1, min(columns, 3))
    builder = InlineKeyboardBuilder()
    for button in buttons:
        builder.button(text=button["title"], callback_data=f"pub:{button['id']}")
    builder.adjust(columns)
    return builder.as_markup()


def admin_main(enabled: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✍️ Приветствие", callback_data="adm:greeting"),
        InlineKeyboardButton(text="⏱ Интервал", callback_data="adm:cooldown"),
    )
    builder.row(
        InlineKeyboardButton(text="🧩 Кнопки", callback_data="adm:buttons"),
        InlineKeyboardButton(text="🧱 Сетка", callback_data="adm:grid"),
    )
    builder.row(
        InlineKeyboardButton(
            text=("🟢 Автоответ включён" if enabled else "⚪ Автоответ выключен"),
            callback_data="adm:toggle",
        )
    )
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"))
    return builder.as_markup()


def admin_buttons_list(buttons: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for button in buttons:
        icon = "🟢" if button["enabled"] else "⚪"
        builder.row(
            InlineKeyboardButton(
                text=f"{icon} {button['position']}. {button['title']}",
                callback_data=f"adm:btn:{button['id']}",
            )
        )
    builder.row(InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="adm:add"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home"))
    return builder.as_markup()


def admin_button_edit(button: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Название", callback_data=f"adm:title:{button['id']}"),
        InlineKeyboardButton(text="💬 Ответ", callback_data=f"adm:response:{button['id']}"),
    )
    builder.row(
        InlineKeyboardButton(text="🔢 Позиция", callback_data=f"adm:position:{button['id']}"),
        InlineKeyboardButton(
            text=("⚪ Выключить" if button["enabled"] else "🟢 Включить"),
            callback_data=f"adm:enable:{button['id']}",
        ),
    )
    builder.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm:delete:{button['id']}"))
    builder.row(InlineKeyboardButton(text="⬅️ К списку", callback_data="adm:buttons"))
    return builder.as_markup()


def delete_confirm(button_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Да, удалить", callback_data=f"adm:delete_confirm:{button_id}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"adm:btn:{button_id}"),
    )
    return builder.as_markup()
