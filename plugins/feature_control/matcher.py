from nonebot import get_driver, logger, on_message
from nonebot.adapters.onebot.v11 import Event
from nonebot.rule import Rule

from .addressing import addressed_group_admin_message
from .commands import execute_control_command, is_control_command
from .runtime import FEATURES


async def is_control_event(event: Event) -> bool:
    message = addressed_group_admin_message(event)
    if message is None or any(segment.type == "at" for segment in message):
        return False
    return is_control_command(message.extract_plain_text())


control_matcher = on_message(
    rule=Rule(is_control_event),
    priority=0,
    block=True,
)


@control_matcher.handle()
async def handle_control_command(event: Event) -> None:
    message = addressed_group_admin_message(event)
    if message is None or any(segment.type == "at" for segment in message):
        return
    if str(event.user_id) not in {str(user_id) for user_id in get_driver().config.superusers}:
        await control_matcher.finish("你没有模块管理权限。")
        return
    try:
        reply = execute_control_command(
            message.extract_plain_text(), FEATURES, str(event.user_id)
        )
    except OSError as exc:
        logger.error(f"模块状态写入失败 error={type(exc).__name__}")
        await control_matcher.finish("写入失败，状态未改变。")
        return
    await control_matcher.finish(reply)
