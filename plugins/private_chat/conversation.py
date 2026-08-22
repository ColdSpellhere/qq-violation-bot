import asyncio
import weakref
from collections import deque
from typing import TYPE_CHECKING

from plugins.chat_archive.db import ContextMessage

if TYPE_CHECKING:
    from plugins.private_memory.store import PrivateMemoryStore, PrivateUserEventState

_USER_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _user_lock(user_id: str) -> asyncio.Lock:
    if not user_id:
        return asyncio.Lock()
    lock = _USER_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _USER_LOCKS[user_id] = lock
    return lock


class PrivateConversation:
    def __init__(
        self,
        limit: int = 20,
        *,
        user_id: str = "",
        store: "PrivateMemoryStore | None" = None,
    ):
        self._limit = limit
        self.user_id = user_id
        self.store = store
        self._turns: deque[ContextMessage] = deque(maxlen=limit)
        self.lock = _user_lock(user_id)

    def use_store(self, store: "PrivateMemoryStore | None") -> None:
        self.store = store

    def append(self, turn: ContextMessage) -> None:
        self._turns.append(turn)

    def append_user(self, turn: ContextMessage, *, event_time: int) -> int | None:
        watermark, _ = self.append_user_with_status(turn, event_time=event_time)
        return watermark

    def append_user_with_status(
        self, turn: ContextMessage, *, event_time: int
    ) -> tuple[int | None, bool]:
        state = self.append_user_state(turn, event_time=event_time)
        return ((state.row_id, state.created) if state is not None else (None, True))

    def append_user_state(
        self, turn: ContextMessage, *, event_time: int
    ) -> "PrivateUserEventState | None":
        state = None
        if self.store is not None:
            state = self.store.append_user_message_state(
                user_id=self.user_id,
                message_id=turn.message_id,
                text=turn.text,
                event_time=event_time,
                source_kind="text",
            )
        self.append(turn)
        return state

    def append_assistant(self, turn: ContextMessage, *, event_time: int) -> int | None:
        watermark = None
        if self.store is not None:
            source_message_id = turn.message_id.removeprefix("bot:")
            watermark = self.store.append_assistant_message(
                user_id=self.user_id,
                source_message_id=source_message_id,
                bot_user_id=turn.user_id,
                text=turn.text,
                event_time=event_time,
            )
        self.append(turn)
        return watermark

    def snapshot(self) -> tuple[ContextMessage, ...]:
        if self.store is not None:
            return self.store.recent_context(user_id=self.user_id, limit=self._limit)
        return tuple(self._turns)
