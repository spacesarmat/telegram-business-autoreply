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
