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


def form_question_nav(required: bool, can_go_back: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []
    if can_go_back:
        row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data="form:back"))
    if not required:
        row.append(InlineKeyboardButton(text="⏭ Пропустить", callback_data="form:skip"))
    if row:
        builder.row(*row)
    builder.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="form:menu"))
    builder.row(InlineKeyboardButton(text="❌ Отменить заявку", callback_data="form:cancel"))
    return builder.as_markup()


def form_confirmation() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Отправить заявку", callback_data="form:submit"))
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить ответы", callback_data="form:edit"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="form:cancel"),
    )
    builder.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="form:menu"))
    return builder.as_markup()


def admin_main(enabled: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✍️ Приветствие", callback_data="adm:greeting"),
        InlineKeyboardButton(text="⏱ Интервал", callback_data="adm:cooldown"),
    )
    builder.row(
        InlineKeyboardButton(text="🧩 Кнопки", callback_data="adm:buttons"),
        InlineKeyboardButton(text="📝 Формы", callback_data="adm:forms"),
    )
    builder.row(
        InlineKeyboardButton(text="🧱 Сетка", callback_data="adm:grid"),
        InlineKeyboardButton(text="⚡ Вызов меню", callback_data="adm:menu_triggers"),
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


def admin_forms_list(forms: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for form in forms:
        icon = "🟢" if form["enabled"] else "⚪"
        builder.row(
            InlineKeyboardButton(
                text=f"{icon} {form['name']} · {form['question_count']} вопр.",
                callback_data=f"adm:form:{form['id']}",
            )
        )
    builder.row(InlineKeyboardButton(text="➕ Создать форму", callback_data="adm:form_add"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home"))
    return builder.as_markup()


def admin_form_edit(form: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Название", callback_data=f"adm:form_name:{form['id']}"),
        InlineKeyboardButton(
            text=("⚪ Выключить" if form["enabled"] else "🟢 Включить"),
            callback_data=f"adm:form_enable:{form['id']}",
        ),
    )
    builder.row(
        InlineKeyboardButton(text="❓ Вопросы", callback_data=f"adm:form_questions:{form['id']}"),
        InlineKeyboardButton(text="🔗 Кнопки", callback_data=f"adm:form_bind:{form['id']}"),
    )
    builder.row(InlineKeyboardButton(text="🗑 Удалить форму", callback_data=f"adm:form_delete:{form['id']}"))
    builder.row(InlineKeyboardButton(text="⬅️ К формам", callback_data="adm:forms"))
    return builder.as_markup()


def admin_form_delete_confirm(form_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Да, удалить", callback_data=f"adm:form_delete_yes:{form_id}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"adm:form:{form_id}"),
    )
    return builder.as_markup()


def admin_form_questions(form_id: int, questions: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for q in questions:
        req = "*" if q["required"] else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{q['position']}. {q['label']}{req}",
                callback_data=f"adm:q:{q['id']}",
            )
        )
    builder.row(InlineKeyboardButton(text="➕ Добавить вопрос", callback_data=f"adm:q_add:{form_id}"))
    builder.row(InlineKeyboardButton(text="⬅️ К форме", callback_data=f"adm:form:{form_id}"))
    return builder.as_markup()


def admin_question_edit(question: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🏷 Поле", callback_data=f"adm:q_label:{question['id']}"),
        InlineKeyboardButton(text="💬 Вопрос", callback_data=f"adm:q_prompt:{question['id']}"),
    )
    builder.row(
        InlineKeyboardButton(text="🔢 Позиция", callback_data=f"adm:q_pos:{question['id']}"),
        InlineKeyboardButton(
            text=("✅ Обязательный" if question["required"] else "⏭ Необязательный"),
            callback_data=f"adm:q_required:{question['id']}",
        ),
    )
    builder.row(InlineKeyboardButton(text="🗑 Удалить вопрос", callback_data=f"adm:q_delete:{question['id']}"))
    builder.row(
        InlineKeyboardButton(
            text="⬅️ К вопросам",
            callback_data=f"adm:form_questions:{question['form_id']}",
        )
    )
    return builder.as_markup()


def admin_question_delete_confirm(question: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Да, удалить", callback_data=f"adm:q_delete_yes:{question['id']}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"adm:q:{question['id']}"),
    )
    return builder.as_markup()


def admin_form_bindings(form_id: int, buttons: list[dict], bound_ids: set[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for button in buttons:
        icon = "✅" if int(button["id"]) in bound_ids else "▫️"
        builder.row(
            InlineKeyboardButton(
                text=f"{icon} {button['title']}",
                callback_data=f"adm:form_bind_btn:{form_id}:{button['id']}",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ К форме", callback_data=f"adm:form:{form_id}"))
    return builder.as_markup()
