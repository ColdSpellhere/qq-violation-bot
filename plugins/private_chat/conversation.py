import asyncio
from collections import deque

from plugins.chat_archive.db import ContextMessage


class PrivateConversation:
    def __init__(self, limit: int = 20):
        self._turns: deque[ContextMessage] = deque(maxlen=limit)
        self.lock = asyncio.Lock()

    def append(self, turn: ContextMessage) -> None:
        self._turns.append(turn)

    def snapshot(self) -> tuple[ContextMessage, ...]:
        return tuple(self._turns)
