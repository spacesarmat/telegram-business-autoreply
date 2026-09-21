from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    greeting = State()
    cooldown = State()
    grid_columns = State()
    menu_triggers = State()

    add_title = State()
    add_response = State()

    edit_title = State()
    edit_response = State()
    edit_position = State()

    form_add_name = State()
    form_edit_name = State()

    question_add_label = State()
    question_add_prompt = State()
    question_edit_label = State()
    question_edit_prompt = State()
    question_edit_position = State()
    question_edit_options = State()

    availability_date = State()
    availability_period = State()

    submission_search = State()
    submission_amount = State()
    submission_prepayment = State()
    submission_note = State()
    status_template = State()

    pricing_base_amount = State()
    pricing_base_description = State()
    pricing_included_hours = State()
    pricing_extra_hour_amount = State()
    pricing_buffer_before = State()
    pricing_buffer_after = State()

    addon_add_name = State()
    addon_add_amount = State()
    addon_edit_name = State()
    addon_edit_amount = State()
    addon_edit_quantity_limits = State()
