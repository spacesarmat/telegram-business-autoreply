from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class UpdateConcurrencyGuard:
    """Serialize updates per chat while keeping different chats concurrent.

    The global semaphore limits the number of handlers that can actively execute
    at once. Per-chat locks prevent two messages/callbacks for the same dialog
    from reading and updating the same form session concurrently.
    """

    def __init__(self, max_concurrent: int = 32) -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self._global = asyncio.Semaphore(self.max_concurrent)
        self._registry_lock = asyncio.Lock()
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._chat_refs: dict[int, int] = {}
        self._callback_lock = asyncio.Lock()
        self._inflight_callbacks: set[tuple[int, int, int, str]] = set()
        self._active = 0
        self._waiting = 0
        self._peak_active = 0
        self._peak_waiting = 0

    @staticmethod
    def _chat_id(event: TelegramObject) -> int | None:
        if isinstance(event, Message):
            return int(event.chat.id)
        if isinstance(event, CallbackQuery) and event.message is not None:
            chat = getattr(event.message, "chat", None)
            if chat is not None:
                return int(chat.id)
        return None

    @staticmethod
    def _callback_signature(event: TelegramObject) -> tuple[int, int, int, str] | None:
        if not isinstance(event, CallbackQuery) or event.message is None:
            return None
        chat = getattr(event.message, "chat", None)
        message_id = getattr(event.message, "message_id", None)
        if chat is None or message_id is None or event.from_user is None:
            return None
        return (
            int(chat.id),
            int(message_id),
            int(event.from_user.id),
            str(event.data or ""),
        )

    async def _reserve_chat_lock(self, chat_id: int) -> asyncio.Lock:
        async with self._registry_lock:
            lock = self._chat_locks.get(chat_id)
            if lock is None:
                lock = asyncio.Lock()
                self._chat_locks[chat_id] = lock
                self._chat_refs[chat_id] = 0
            self._chat_refs[chat_id] = self._chat_refs.get(chat_id, 0) + 1
            return lock

    async def _release_chat_ref(self, chat_id: int, lock: asyncio.Lock) -> None:
        async with self._registry_lock:
            refs = self._chat_refs.get(chat_id, 1) - 1
            if refs <= 0 and not lock.locked():
                self._chat_refs.pop(chat_id, None)
                if self._chat_locks.get(chat_id) is lock:
                    self._chat_locks.pop(chat_id, None)
            else:
                self._chat_refs[chat_id] = max(0, refs)

    def snapshot(self) -> dict[str, int]:
        return {
            "active": self._active,
            "waiting": self._waiting,
            "chat_locks": len(self._chat_locks),
            "inflight_callbacks": len(self._inflight_callbacks),
            "max_concurrent": self.max_concurrent,
            "peak_active": self._peak_active,
            "peak_waiting": self._peak_waiting,
        }

    async def run(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        signature = self._callback_signature(event)
        if signature is not None:
            async with self._callback_lock:
                if signature in self._inflight_callbacks:
                    try:
                        await event.answer("Уже обрабатываю это действие…")
                    except Exception:
                        logger.debug("Failed to answer duplicate callback", exc_info=True)
                    return None
                self._inflight_callbacks.add(signature)

        chat_id = self._chat_id(event)
        chat_lock: asyncio.Lock | None = None
        chat_acquired = False
        global_acquired = False
        handler_started = False
        self._waiting += 1
        self._peak_waiting = max(self._peak_waiting, self._waiting)
        try:
            if chat_id is not None:
                chat_lock = await self._reserve_chat_lock(chat_id)
                await chat_lock.acquire()
                chat_acquired = True

            await self._global.acquire()
            global_acquired = True
            self._waiting = max(0, self._waiting - 1)
            handler_started = True
            self._active += 1
            self._peak_active = max(self._peak_active, self._active)
            return await handler(event, data)
        finally:
            if handler_started:
                self._active = max(0, self._active - 1)
            else:
                self._waiting = max(0, self._waiting - 1)
            if global_acquired:
                self._global.release()
            if chat_acquired and chat_lock is not None:
                chat_lock.release()
            if chat_lock is not None and chat_id is not None:
                await self._release_chat_ref(chat_id, chat_lock)
            if signature is not None:
                async with self._callback_lock:
                    self._inflight_callbacks.discard(signature)



class ConcurrencyMiddleware(BaseMiddleware):
    def __init__(self, guard: UpdateConcurrencyGuard) -> None:
        self.guard = guard

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        return await self.guard.run(handler, event, data)
