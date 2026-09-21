from __future__ import annotations


TEXTS = {
    "ru": {
        "direct_menu": "Выберите действие в личном чате с ботом:",
        "game_title": "🎮 Крестики-нолики",
        "game_turn": "Вы играете крестиками X. Выберите свободную клетку.",
        "game_x": "Вы победили! Вы играете крестиками X.",
        "game_o": "Бот победил. Попробуем ещё раз?",
        "game_draw": "Ничья. Попробуем ещё раз?",
    },
    "en": {
        "direct_menu": "Choose an action in the direct chat with the bot:",
        "game_title": "🎮 Tic-tac-toe",
        "game_turn": "You are X. Choose an empty cell.",
        "game_x": "You won! You are X.",
        "game_o": "The bot won. Try again?",
        "game_draw": "Draw. Try again?",
    },
}


def language(value: str | None) -> str:
    key = str(value or "ru").split("-", 1)[0].casefold()
    return key if key in TEXTS else "ru"


def tr(key: str, lang: str | None = None) -> str:
    selected = language(lang)
    return TEXTS[selected].get(key, TEXTS["ru"].get(key, key))
