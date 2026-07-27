from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.violation_record import moderation


class MuteSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_mute_returns_before_validation_or_onebot_calls(self) -> None:
        bot = AsyncMock()
        bot.set_group_ban = AsyncMock()
        bot.call_api = AsyncMock()

        with patch.object(
            moderation,
            "CONFIG",
            SimpleNamespace(mute_enabled=False),
            create=True,
        ):
            result = await moderation.handle_mute_intent(
                bot,
                {},
                group_id="123456789",
                operator_qq="90001",
                operator_nickname="记录员",
                message_id="201",
            )

        self.assertEqual("禁言功能未启用。", result)
        bot.set_group_ban.assert_not_awaited()
        bot.call_api.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
