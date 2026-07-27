from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from plugins.violation_record.matcher import _send_structured_reply
from plugins.violation_record.reply_models import RecordMessage, StructuredReply


class ReplyModelTests(unittest.TestCase):
    def test_record_keeps_all_images_in_order(self) -> None:
        reply = StructuredReply(
            records=(RecordMessage("1. record", (Path("a.jpg"), Path("b.png"))),)
        )
        self.assertEqual((Path("a.jpg"), Path("b.png")), reply.records[0].images)

    def test_old_record_has_empty_image_tuple(self) -> None:
        self.assertEqual((), RecordMessage("1. old").images)


class StructuredReplyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.first = Path(self.temp.name) / "first.jpg"
        self.second = Path(self.temp.name) / "second.png"
        self.first.write_bytes(b"first")
        self.second.write_bytes(b"second")
        self.reply = StructuredReply(
            records=(RecordMessage("1. record", (self.first, self.second)),)
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_record_and_images_use_one_mixed_message(self) -> None:
        bot = AsyncMock()

        await _send_structured_reply(bot, 123456789, self.reply)

        bot.send_group_msg.assert_awaited_once()
        call = bot.send_group_msg.await_args
        self.assertEqual(123456789, call.kwargs["group_id"])
        message = call.kwargs["message"]
        self.assertEqual(["text", "image", "image"], [segment.type for segment in message])
        self.assertEqual("1. record", message[0].data["text"])
        self.assertEqual(f"file://{self.first}", message[1].data["file"])
        self.assertEqual(f"file://{self.second}", message[2].data["file"])

    async def test_mixed_failure_falls_back_and_one_image_failure_does_not_stop_next(self) -> None:
        bot = AsyncMock()
        bot.send_group_msg.side_effect = [RuntimeError("mixed"), None, RuntimeError("first"), None]

        await _send_structured_reply(bot, 123456789, self.reply)

        self.assertEqual(4, bot.send_group_msg.await_count)
        calls = bot.send_group_msg.await_args_list
        self.assertEqual("1. record", calls[1].kwargs["message"])
        self.assertEqual("image", calls[2].kwargs["message"].type)
        self.assertEqual(f"file://{self.first}", calls[2].kwargs["message"].data["file"])
        self.assertEqual("image", calls[3].kwargs["message"].type)
        self.assertEqual(f"file://{self.second}", calls[3].kwargs["message"].data["file"])


if __name__ == "__main__":
    unittest.main()
