from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    greeting = State()
    cooldown = State()
    grid_columns = State()

    add_title = State()
    add_response = State()

    edit_title = State()
    edit_response = State()
    edit_position = State()
