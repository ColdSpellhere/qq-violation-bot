import asyncio
import unittest
from unittest.mock import AsyncMock

from plugins.member_memory.batcher import MemberMemoryBatcher


class MemberMemoryBatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifth_message_flushes_immediately(self):
        callback = AsyncMock()
        batcher = MemberMemoryBatcher(threshold=5, delay_seconds=60)

        for event_time in range(1000, 1005):
            batcher.add(
                group_id=123,
                user_id="456",
                event_time=event_time,
                callback=callback,
            )
        await asyncio.sleep(0)

        callback.assert_awaited_once_with(123, "456", 1004)
        await batcher.drain()

    async def test_single_message_flushes_after_delay(self):
        callback = AsyncMock()
        batcher = MemberMemoryBatcher(threshold=5, delay_seconds=0.02)

        batcher.add(group_id=123, user_id="456", event_time=1000, callback=callback)
        await batcher.drain()

        callback.assert_awaited_once_with(123, "456", 1000)

    async def test_different_members_flush_independently(self):
        callback = AsyncMock()
        batcher = MemberMemoryBatcher(threshold=5, delay_seconds=0.02)

        batcher.add(group_id=123, user_id="456", event_time=1000, callback=callback)
        batcher.add(group_id=123, user_id="789", event_time=1001, callback=callback)
        await batcher.drain()

        self.assertEqual(2, callback.await_count)
        callback.assert_any_await(123, "456", 1000)
        callback.assert_any_await(123, "789", 1001)

    async def test_batches_for_same_member_run_serially(self):
        active = 0
        maximum_active = 0

        async def callback(group_id, user_id, event_time):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.02)
            active -= 1

        batcher = MemberMemoryBatcher(threshold=1, delay_seconds=60)
        batcher.add(group_id=123, user_id="456", event_time=1000, callback=callback)
        batcher.add(group_id=123, user_id="456", event_time=1001, callback=callback)
        await batcher.drain()

        self.assertEqual(1, maximum_active)


if __name__ == "__main__":
    unittest.main()
