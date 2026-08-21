from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Event
from nonebot.rule import Rule

from .commands import execute_control_command, is_control_command
from .runtime import FEATURES


async def is_control_event(event: Event) -> bool:
    return is_control_command(event.get_plaintext())


control_matcher = on_message(
    rule=Rule(is_control_event),
    priority=0,
    block=True,
)


@control_matcher.handle()
async def handle_control_command(event: Event) -> None:
    if str(event.user_id) not in {str(user_id) for user_id in get_driver().config.superusers}:
        await control_matcher.finish("你没有模块管理权限。")
        return
    await control_matcher.finish(
        execute_control_command(event.get_plaintext(), FEATURES, str(event.user_id))
    )
