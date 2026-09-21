from __future__ import annotations

from dataclasses import dataclass, field

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from .i18n import tr


WIN_LINES = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
)


def winner(board: list[str]) -> str | None:
    for a, b, c in WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return "draw" if all(board) else None


def bot_move(board: list[str]) -> int | None:
    """Deterministic, unbeatable minimax move for O."""
    empty = [i for i, value in enumerate(board) if not value]
    if not empty:
        return None

    # Prefer an immediate win, then block an immediate player win. Besides
    # feeling natural, this also makes equal-score minimax branches stable.
    for mark in ("O", "X"):
        for cell in empty:
            board[cell] = mark
            is_win = winner(board) == mark
            board[cell] = ""
            if is_win:
                return cell

    def score(state: list[str], maximizing: bool) -> int:
        result = winner(state)
        if result == "O":
            return 10
        if result == "X":
            return -10
        if result == "draw":
            return 0
        values: list[int] = []
        mark = "O" if maximizing else "X"
        for cell in range(9):
            if state[cell]:
                continue
            state[cell] = mark
            values.append(score(state, not maximizing))
            state[cell] = ""
        return max(values) if maximizing else min(values)

    priority = (4, 0, 2, 6, 8, 1, 3, 5, 7)
    best_value = -100
    best_cell = empty[0]
    for cell in priority:
        if board[cell]:
            continue
        board[cell] = "O"
        value = score(board, False)
        board[cell] = ""
        if value > best_value:
            best_value, best_cell = value, cell
    return best_cell


@dataclass
class Game:
    board: list[str] = field(default_factory=lambda: [""] * 9)
    finished: bool = False


class TicTacToeStore:
    def __init__(self) -> None:
        self._games: dict[int, Game] = {}

    def reset(self, chat_id: int) -> Game:
        game = Game()
        self._games[chat_id] = game
        return game

    def get(self, chat_id: int) -> Game:
        return self._games.setdefault(chat_id, Game())

    def play(self, chat_id: int, cell: int) -> tuple[Game, str | None]:
        game = self.get(chat_id)
        if game.finished or cell not in range(9) or game.board[cell]:
            return game, "invalid"
        game.board[cell] = "X"
        result = winner(game.board)
        if not result:
            move = bot_move(game.board)
            if move is not None:
                game.board[move] = "O"
            result = winner(game.board)
        game.finished = result is not None
        return game, result


def game_keyboard(game: Game) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for row in range(3):
        buttons: list[InlineKeyboardButton] = []
        for col in range(3):
            cell = row * 3 + col
            value = game.board[cell] or "·"
            buttons.append(InlineKeyboardButton(text=value, callback_data=f"game:cell:{cell}"))
        rows.append(buttons)
    rows.append([InlineKeyboardButton(text="🔄 Новая игра", callback_data="game:new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def game_text(result: str | None = None, lang: str | None = None) -> str:
    if result == "X":
        return f"{tr('game_title', lang)}\n\n{tr('game_x', lang)}"
    if result == "O":
        return f"{tr('game_title', lang)}\n\n{tr('game_o', lang)}"
    if result == "draw":
        return f"{tr('game_title', lang)}\n\n{tr('game_draw', lang)}"
    return f"{tr('game_title', lang)}\n\n{tr('game_turn', lang)}"
