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
        self.missing = Path(self.temp.name) / "missing.jpg"
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
        bot.call_api.assert_not_awaited()
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

    async def test_multiple_records_use_one_merged_forward_with_ordered_images(self) -> None:
        bot = AsyncMock()
        bot.self_id = "10000"
        reply = StructuredReply(
            records=(
                RecordMessage("查询标题\n\n1. first", (self.first, self.missing)),
                RecordMessage("2. second", (self.second,)),
            )
        )

        await _send_structured_reply(bot, 123456789, reply)

        bot.call_api.assert_awaited_once()
        call = bot.call_api.await_args
        self.assertEqual("send_group_forward_msg", call.args[0])
        self.assertEqual(123456789, call.kwargs["group_id"])
        nodes = call.kwargs["messages"]
        self.assertEqual(2, len(nodes))
        self.assertTrue(all(node.type == "node" for node in nodes))
        self.assertEqual("10000", nodes[0].data["user_id"])
        self.assertEqual("违规记录机器人", nodes[0].data["nickname"])
        first_content = nodes[0].data["content"]
        second_content = nodes[1].data["content"]
        self.assertEqual(["text", "image"], [segment.type for segment in first_content])
        self.assertEqual("查询标题\n\n1. first", first_content[0].data["text"])
        self.assertEqual(f"file://{self.first}", first_content[1].data["file"])
        self.assertEqual(["text", "image"], [segment.type for segment in second_content])
        self.assertEqual("2. second", second_content[0].data["text"])
        self.assertEqual(f"file://{self.second}", second_content[1].data["file"])
        bot.send_group_msg.assert_not_awaited()

    async def test_forward_failure_falls_back_to_one_text_message_without_spam(self) -> None:
        bot = AsyncMock()
        bot.self_id = "10000"
        bot.call_api.side_effect = RuntimeError("forward")
        reply = StructuredReply(
            records=(
                RecordMessage("查询标题\n\n1. first", (self.first,)),
                RecordMessage("2. second", (self.second,)),
            )
        )

        await _send_structured_reply(bot, 123456789, reply)

        bot.call_api.assert_awaited_once()
        bot.send_group_msg.assert_awaited_once_with(
            group_id=123456789,
            message=(
                "查询标题\n\n1. first\n\n2. second\n\n"
                "合并转发发送失败，已改为单条文字；证据图片未展开，请稍后重试。"
            ),
        )


if __name__ == "__main__":
    unittest.main()
