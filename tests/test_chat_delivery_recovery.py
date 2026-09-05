from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from plugins.random_chat.delivery import deliver_replies


class DeliveryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_send_failed_archive_recovers_without_resending(self):
        from plugins.random_chat.delivery_store import DeliveryLedger
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat_delivery.sqlite3"
            ledger = DeliveryLedger(path)
            send = AsyncMock(return_value={"message_id": "42"})
            archive = AsyncMock(side_effect=OSError("synthetic disk failure"))
            await deliver_replies(("first", "second"), send=send,
                ledger=ledger, delivery_key="event", after_send=archive, interval=0)
            self.assertEqual(send.await_count, 1)
            self.assertEqual(ledger.parts("event")[0]["status"], "sent")
            restored = DeliveryLedger(path)
            archive = AsyncMock()
            await deliver_replies(("changed must not be used",), send=send,
                ledger=restored, delivery_key="event", after_send=archive, interval=0)
            self.assertEqual(send.await_count, 2)
            self.assertEqual(send.await_args.args, ("second",))
            self.assertEqual([call.args for call in archive.await_args_list], [("first", 0), ("second", 1)])
            self.assertTrue(all(row["status"] == "archived" for row in restored.parts("event")))

    async def test_cancelled_or_ambiguous_send_is_not_blindly_repeated(self):
        from plugins.random_chat.delivery_store import DeliveryLedger
        with tempfile.TemporaryDirectory() as directory:
            ledger = DeliveryLedger(Path(directory) / "delivery.sqlite3")
            send = AsyncMock(side_effect=asyncio.CancelledError())
            with self.assertRaises(asyncio.CancelledError):
                await deliver_replies(("reply",), send=send, ledger=ledger, delivery_key="event")
            self.assertEqual(ledger.parts("event")[0]["status"], "unknown")
            await deliver_replies(("reply",), send=send, ledger=ledger, delivery_key="event")
            self.assertEqual(send.await_count, 1)

    async def test_crash_after_claim_and_duplicate_completion_never_resend(self):
        from plugins.random_chat.delivery_store import DeliveryLedger
        with tempfile.TemporaryDirectory() as directory:
            ledger = DeliveryLedger(Path(directory) / "delivery.sqlite3")
            ledger.plan("event", ("one",), kind="group", user_id="user", group_id="1")
            self.assertTrue(ledger.claim("event", 0))
            send = AsyncMock()
            await deliver_replies(("one",), send=send, ledger=ledger, delivery_key="event")
            send.assert_not_awaited()

    async def test_delivery_checks_feature_gate_before_claiming_send(self):
        from plugins.random_chat.delivery_store import DeliveryLedger
        with tempfile.TemporaryDirectory() as directory:
            ledger = DeliveryLedger(Path(directory) / "delivery.sqlite3")
            send = AsyncMock()
            await deliver_replies(("one",), send=send, ledger=ledger, delivery_key="event", allowed=lambda: False)
            send.assert_not_awaited()
            self.assertEqual(ledger.parts("event")[0]["status"], "pending")

    async def test_clear_during_send_cannot_resurrect_private_reply(self):
        from plugins.random_chat.delivery_store import DeliveryLedger
        with tempfile.TemporaryDirectory() as directory:
            ledger = DeliveryLedger(Path(directory) / "delivery.sqlite3")
            async def send(_):
                with ledger._connect() as db:
                    db.execute("UPDATE chat_delivery_parts SET reply_text='',status='cancelled' WHERE user_id='user'")
                return {"message_id": "42"}
            archive = AsyncMock()
            await deliver_replies(("one",), send=send, ledger=ledger, delivery_key="event",
                kind="private", user_id="user", after_send=archive)
            archive.assert_not_awaited()
            self.assertEqual(ledger.parts("event")[0]["reply_text"], "")
            self.assertEqual(ledger.parts("event")[0]["status"], "cancelled")

    async def test_feature_disabled_between_parts_prevents_second_send(self):
        enabled = True
        calls = []
        async def send(value):
            nonlocal enabled
            calls.append(value)
            enabled = False
        await deliver_replies(("one", "two"), send=send, allowed=lambda: enabled, interval=0)
        self.assertEqual(calls, ["one"])


if __name__ == "__main__":
    unittest.main()
