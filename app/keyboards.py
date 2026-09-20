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


MONTH_NAMES_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def calendar_keyboard(
    question_id: int,
    year: int,
    month: int,
    weeks: list[list[int]],
    *,
    required: bool,
    can_go_back: bool,
    today_iso: str,
    full_busy_dates: set[str] | None = None,
    partial_busy_dates: set[str] | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    full_busy_dates = full_busy_dates or set()
    partial_busy_dates = partial_busy_dates or set()
    builder.row(
        InlineKeyboardButton(
            text=f"{MONTH_NAMES_RU[month - 1]} {year}", callback_data="cal:noop"
        )
    )
    builder.row(
        *[InlineKeyboardButton(text=day, callback_data="cal:noop") for day in WEEKDAYS_RU]
    )
    for week in weeks:
        row: list[InlineKeyboardButton] = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text="·", callback_data="cal:noop"))
                continue
            iso = f"{year:04d}-{month:02d}-{day:02d}"
            if iso in full_busy_dates:
                label = f"×{day}"
                callback_data = f"cal:busy:{question_id}:{iso}"
            elif iso in partial_busy_dates:
                label = f"•{day}"
                callback_data = f"cal:day:{question_id}:{iso}"
            else:
                label = f"•{day}" if iso == today_iso else str(day)
                callback_data = f"cal:day:{question_id}:{iso}"
            row.append(InlineKeyboardButton(text=label, callback_data=callback_data))
        builder.row(*row)

    prev_month = month - 1
    prev_year = year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1
    next_month = month + 1
    next_year = year
    if next_month == 13:
        next_month = 1
        next_year += 1
    builder.row(
        InlineKeyboardButton(
            text="◀️", callback_data=f"cal:nav:{question_id}:{prev_year:04d}-{prev_month:02d}"
        ),
        InlineKeyboardButton(
            text="Сегодня", callback_data=f"cal:today:{question_id}"
        ),
        InlineKeyboardButton(
            text="▶️", callback_data=f"cal:nav:{question_id}:{next_year:04d}-{next_month:02d}"
        ),
    )
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
    builder.row(
        InlineKeyboardButton(text="📋 Заявки", callback_data="adm:reqs:all"),
        InlineKeyboardButton(text="📅 Занятость", callback_data="adm:availability"),
    )
    builder.row(
        InlineKeyboardButton(text="💬 Шаблоны статусов", callback_data="adm:status_templates"),
        InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
    )
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
    type_labels = {"text": "⌨️ Текст", "date": "📅 Дата", "time": "🕐 Время", "contact": "📱 Контакт"}
    builder.row(
        InlineKeyboardButton(
            text=f"Тип: {type_labels.get(question.get('input_type', 'text'), '⌨️ Текст')}",
            callback_data=f"adm:q_type_menu:{question['id']}",
        )
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

def admin_question_type(question_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⌨️ Текст", callback_data=f"adm:q_type:{question_id}:text"),
        InlineKeyboardButton(text="📅 Дата", callback_data=f"adm:q_type:{question_id}:date"),
    )
    builder.row(
        InlineKeyboardButton(text="🕐 Время", callback_data=f"adm:q_type:{question_id}:time"),
        InlineKeyboardButton(text="📱 Контакт", callback_data=f"adm:q_type:{question_id}:contact"),
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm:q:{question_id}")
    )
    return builder.as_markup()



SUBMISSION_STATUS_LABELS = {
    "new": "🆕 Новая",
    "in_progress": "🟡 В работе",
    "confirmed": "✅ Подтверждена",
    "paid": "💰 Оплачена",
    "completed": "🏁 Завершена",
    "cancelled": "❌ Отказ",
}


def time_slots_keyboard(
    question_id: int,
    slots: list[str],
    busy_slots: set[str],
    *,
    required: bool,
    can_go_back: bool,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for slot in slots:
        compact = slot.replace(":", "")
        if slot in busy_slots:
            builder.button(text=f"× {slot}", callback_data=f"time:busy:{question_id}:{compact}")
        else:
            builder.button(text=slot, callback_data=f"time:pick:{question_id}:{compact}")
    builder.adjust(3)
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


def end_time_slots_keyboard(
    question_id: int,
    options: list[tuple[str, str]],
    busy_values: set[str],
    *,
    required: bool,
    can_go_back: bool,
) -> InlineKeyboardMarkup:
    """Keyboard for event end time, including next-day labels such as 05:00 +1д."""
    builder = InlineKeyboardBuilder()
    for value, label in options:
        compact = value.replace(":", "")
        if value in busy_values:
            builder.button(text=f"× {label}", callback_data=f"time:busy:{question_id}:{compact}")
        else:
            builder.button(text=label, callback_data=f"time:pick:{question_id}:{compact}")
    builder.adjust(3)
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


def admin_submissions_list(submissions: list[dict], current_filter: str = "all") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Все", callback_data="adm:reqs:all"),
        InlineKeyboardButton(text="🆕 Новые", callback_data="adm:reqs:new"),
        InlineKeyboardButton(text="🟡 В работе", callback_data="adm:reqs:in_progress"),
    )
    builder.row(
        InlineKeyboardButton(text="✅ Подтв.", callback_data="adm:reqs:confirmed"),
        InlineKeyboardButton(text="💰 Оплач.", callback_data="adm:reqs:paid"),
        InlineKeyboardButton(text="❌ Отказ", callback_data="adm:reqs:cancelled"),
    )
    builder.row(InlineKeyboardButton(text="🔎 Поиск заявки / клиента", callback_data="adm:req_search"))
    for item in submissions:
        status = str(item.get("status") or "new")
        icon = SUBMISSION_STATUS_LABELS.get(status, "•").split(" ", 1)[0]
        created = str(item.get("created_at") or "")[:10]
        builder.row(
            InlineKeyboardButton(
                text=f"{icon} №{item['id']} · {str(item['form_name'])[:24]} · {created}",
                callback_data=f"adm:req:{item['id']}",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home"))
    return builder.as_markup()


def admin_submission_card(submission: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    sid = int(submission["id"])
    builder.row(
        InlineKeyboardButton(text="🆕 Новая", callback_data=f"adm:req_status:{sid}:new"),
        InlineKeyboardButton(text="🟡 В работе", callback_data=f"adm:req_status:{sid}:in_progress"),
    )
    builder.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm:req_status:{sid}:confirmed"),
        InlineKeyboardButton(text="💰 Оплачено", callback_data=f"adm:req_status:{sid}:paid"),
    )
    builder.row(
        InlineKeyboardButton(text="🏁 Завершить", callback_data=f"adm:req_status:{sid}:completed"),
        InlineKeyboardButton(text="❌ Отказ", callback_data=f"adm:req_status:{sid}:cancelled"),
    )
    builder.row(
        InlineKeyboardButton(text="💵 Стоимость", callback_data=f"adm:req_amount:{sid}"),
        InlineKeyboardButton(text="💳 Предоплата", callback_data=f"adm:req_prepayment:{sid}"),
    )
    builder.row(
        InlineKeyboardButton(text="🗒 Заметка", callback_data=f"adm:req_note:{sid}"),
        InlineKeyboardButton(text="👤 История клиента", callback_data=f"adm:req_history:{sid}"),
    )
    username = (submission.get("username") or "").strip()
    if username:
        builder.row(InlineKeyboardButton(text="💬 Открыть чат", url=f"https://t.me/{username}"))
    builder.row(InlineKeyboardButton(text="⬅️ К заявкам", callback_data="adm:reqs:all"))
    return builder.as_markup()


def admin_status_templates() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🆕 Новая", callback_data="adm:status_tpl:new"),
        InlineKeyboardButton(text="🟡 В работе", callback_data="adm:status_tpl:in_progress"),
    )
    builder.row(
        InlineKeyboardButton(text="✅ Подтверждена", callback_data="adm:status_tpl:confirmed"),
        InlineKeyboardButton(text="💰 Оплачена", callback_data="adm:status_tpl:paid"),
    )
    builder.row(
        InlineKeyboardButton(text="🏁 Завершена", callback_data="adm:status_tpl:completed"),
        InlineKeyboardButton(text="❌ Отказ", callback_data="adm:status_tpl:cancelled"),
    )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home"))
    return builder.as_markup()


def admin_status_template_edit(status: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✏️ Изменить шаблон", callback_data=f"adm:status_tpl_edit:{status}"))
    builder.row(InlineKeyboardButton(text="♻️ По умолчанию", callback_data=f"adm:status_tpl_default:{status}"))
    builder.row(InlineKeyboardButton(text="⬅️ К шаблонам", callback_data="adm:status_templates"))
    return builder.as_markup()


def admin_availability(blocks: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Заблокировать дату/время", callback_data="adm:availability_add"))
    for block in blocks:
        date = str(block["date_iso"])
        period = "весь день" if not block.get("start_time") else f"{block['start_time']}–{block['end_time']}"
        source = " · заявка" if block.get("source_submission_id") else ""
        builder.row(
            InlineKeyboardButton(
                text=f"🗑 {date} · {period}{source}",
                callback_data=f"adm:availability_del:{block['id']}",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home"))
    return builder.as_markup()
