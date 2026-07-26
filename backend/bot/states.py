from aiogram.fsm.state import State, StatesGroup


class Registration(StatesGroup):
    choosing_time = State()
    choosing_focus = State()
    group_binding = State()
