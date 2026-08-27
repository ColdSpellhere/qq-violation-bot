from __future__ import annotations

import unittest
from unittest.mock import AsyncMock


class ChatDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_in_order_waits_between_and_stops_on_failure(self):
        from plugins.random_chat.delivery import deliver_replies

        send = AsyncMock(side_effect=[None, RuntimeError("fail"), None])
        sleep = AsyncMock()
        delivered = await deliver_replies(("一", "二", "三"), send=send, sleep=sleep, interval=0.35)
        self.assertEqual(("一",), delivered)
        self.assertEqual(2, send.await_count)
        sleep.assert_awaited_once_with(0.35)

    async def test_final_message_decorator_only_applies_to_last(self):
        from plugins.random_chat.delivery import deliver_replies

        sent = []
        async def send(value): sent.append(value)
        await deliver_replies(("一", "二"), send=send, decorate_final=lambda value: value + "+图", interval=0)
        self.assertEqual(["一", "二+图"], sent)


if __name__ == "__main__":
    unittest.main()
