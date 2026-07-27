from datetime import datetime
from pathlib import Path
import re
from typing import Any

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message, MessageSegment
from nonebot.rule import Rule

from .admin_resolver import grant_admin, grant_admins
from .ai_router import AIRouterError, parse_intent
from .config import CONFIG
from .evidence_capture import capture_referenced_images
from .evidence_store import EvidenceStore
from .moderation import handle_mute_intent
from .reply_models import StructuredReply
from .service import handle_intent


EXPORT_PATH_RE = re.compile(r"(/opt/qq-violation-bot/exports/[^\s，。]+?\.(?:xlsx|csv))")
GROUP_ADMIN_SYNC_SECONDS = 3600
_GROUP_ADMIN_SYNCED_AT: dict[str, float] = {}


def _is_at_me(event: GroupMessageEvent) -> bool:
    try:
        if event.is_tome():
            return True
    except Exception:
        pass
    self_id = CONFIG.bot_self_id or str(event.self_id)
    for seg in event.message:
        if seg.type == "at" and str(seg.data.get("qq")) == self_id:
            return True
    return False


def _plain_without_at(event: GroupMessageEvent) -> str:
    parts: list[str] = []
    self_id = CONFIG.bot_self_id or str(event.self_id)
    for seg in event.message:
        if seg.type == "at":
            qq = str(seg.data.get("qq") or "").strip()
            if qq == self_id:
                continue
            if qq.isdigit():
                parts.append(f"@{qq}")
            continue
        if seg.type == "text":
            parts.append(str(seg.data.get("text", "")))
    return " ".join(parts).strip()


def _mentioned_user_ids(event: GroupMessageEvent) -> list[str]:
    qqs: list[str] = []
    self_id = CONFIG.bot_self_id or str(event.self_id)
    for seg in event.message:
        if seg.type != "at":
            continue
        qq = str(seg.data.get("qq") or "").strip()
        if not qq.isdigit() or qq == self_id or qq in qqs:
            continue
        qqs.append(qq)
    return qqs


def _format_unix_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _reply_segment(event: GroupMessageEvent) -> dict[str, Any] | None:
    for seg in event.message:
        if seg.type == "reply":
            return dict(seg.data)
    return None


async def _referenced_message_time(bot: Bot, event: GroupMessageEvent) -> str | None:
    if event.reply:
        reply_time = _format_unix_time(getattr(event.reply, "time", None))
        if reply_time:
            return reply_time
    reply_data = _reply_segment(event)
    if not reply_data:
        return None
    reply_time = _format_unix_time(reply_data.get("time"))
    if reply_time:
        return reply_time
    reply_id = reply_data.get("id") or reply_data.get("message_id")
    if not reply_id:
        return None
    try:
        message = await bot.call_api("get_msg", message_id=reply_id)
    except Exception as exc:
        logger.warning(f"获取引用消息失败 message_id={reply_id}: {exc}")
        return None
    if isinstance(message, dict):
        return _format_unix_time(message.get("time"))
    return None


async def _send_structured_reply(bot: Bot, group_id: int, reply: StructuredReply) -> None:
    for record in reply.records:
        message = Message(record.text)
        existing_images = [path for path in record.images if path.is_file()]
        for path in existing_images:
            message += MessageSegment.image(file=f"file://{path}")
        try:
            await bot.send_group_msg(group_id=group_id, message=message)
        except Exception as exc:
            logger.warning(
                f"证据混合消息发送失败 stage=query group={group_id} error={type(exc).__name__}"
            )
            await bot.send_group_msg(group_id=group_id, message=record.text)
            for path in existing_images:
                try:
                    await bot.send_group_msg(
                        group_id=group_id,
                        message=MessageSegment.image(file=f"file://{path}"),
                    )
                except Exception as image_exc:
                    logger.warning(
                        f"单张证据发送失败 stage=query group={group_id} error={type(image_exc).__name__}"
                    )


async def _upload_export_files(bot: Bot, group_id: int, reply: str) -> str:
    paths = []
    for matched in EXPORT_PATH_RE.findall(reply):
        path = Path(matched)
        if path.exists() and path.is_file() and path not in paths:
            paths.append(path)
    if not paths:
        return reply
    results: list[str] = []
    for path in paths:
        try:
            await bot.call_api(
                "upload_group_file",
                group_id=str(group_id),
                file=str(path),
                name=path.name,
            )
            results.append(f"文件已上传：{path.name}")
        except Exception as exc:
            logger.warning(f"上传群文件失败 path={path}: {exc}")
            results.append(f"文件上传失败：{path.name}，可从服务器路径下载。原因：{exc}")
    return f"{reply}\n" + "\n".join(results)


def _sender_name(event: GroupMessageEvent) -> str:
    return event.sender.card or event.sender.nickname or str(event.user_id)


async def _sync_group_admins(bot: Bot, group_id: int) -> None:
    group_key = str(group_id)
    now_ts = datetime.now().timestamp()
    if now_ts - _GROUP_ADMIN_SYNCED_AT.get(group_key, 0) < GROUP_ADMIN_SYNC_SECONDS:
        return
    try:
        members = await bot.call_api("get_group_member_list", group_id=str(group_id))
    except Exception as exc:
        logger.warning(f"同步群成员到管理员表失败 group={group_id}: {exc}")
        _GROUP_ADMIN_SYNCED_AT[group_key] = now_ts
        return
    admins: list[tuple[str, str | None]] = []
    if isinstance(members, list):
        for item in members:
            if not isinstance(item, dict):
                continue
            qq_number = item.get("user_id") or item.get("qq")
            if not qq_number:
                continue
            admins.append((str(qq_number), str(item.get("card") or item.get("nickname") or qq_number)))
    count = grant_admins(admins)
    _GROUP_ADMIN_SYNCED_AT[group_key] = now_ts
    logger.info(f"已同步群成员到管理员表 group={group_id} count={count}")


async def only_allowed_group(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent) and int(event.group_id) in CONFIG.allowed_group_ids


matcher = on_message(rule=Rule(only_allowed_group), priority=10, block=True)


@matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    at_me = _is_at_me(event)
    logger.info(
        f"收到允许群消息 group={event.group_id} user={event.user_id} at_me={at_me} message={event.get_plaintext()[:80]!r}"
    )
    if not at_me:
        return
    grant_admin(str(event.user_id), _sender_name(event))
    await _sync_group_admins(bot, int(event.group_id))
    text = _plain_without_at(event)
    if not text:
        await matcher.finish("请发送业务内容。")
    try:
        referenced_time = await _referenced_message_time(bot, event)
        intent = await parse_intent(text, referenced_time=referenced_time)
        intent["_raw"] = text
        if intent.get("intent") == "create_violation":
            try:
                batch_id, evidence_count = await capture_referenced_images(
                    bot,
                    event,
                    EvidenceStore(CONFIG.evidence_database_path, CONFIG.evidence_root),
                    operator_qq=str(event.user_id),
                    command_message_id=str(event.message_id),
                )
            except Exception as exc:
                logger.warning(
                    f"证据采集降级 stage=capture message_id={event.message_id} error={type(exc).__name__}"
                )
                batch_id, evidence_count = None, 0
            intent["_evidence_batch_id"] = batch_id
            intent["_evidence_count"] = evidence_count
        mentioned_qq_numbers = _mentioned_user_ids(event)
        if mentioned_qq_numbers:
            intent["_mentioned_qq_numbers"] = mentioned_qq_numbers
        if referenced_time:
            intent["_reply_time"] = referenced_time
        if intent.get("intent") == "mute_member":
            reply = await handle_mute_intent(
                bot,
                intent,
                group_id=str(event.group_id),
                operator_qq=str(event.user_id),
                operator_nickname=_sender_name(event),
                message_id=str(event.message_id),
            )
        else:
            reply = await handle_intent(
                intent,
                group_id=str(event.group_id),
                operator_qq=str(event.user_id),
                operator_nickname=_sender_name(event),
                message_id=str(event.message_id),
            )
    except AIRouterError as exc:
        reply = str(exc)
    except Exception as exc:
        logger.exception(f"处理群消息失败：{exc}")
        reply = "处理失败，请稍后重试或联系维护者查看日志。"
    if isinstance(reply, StructuredReply):
        await _send_structured_reply(bot, int(event.group_id), reply)
        await matcher.finish()
    reply = await _upload_export_files(bot, int(event.group_id), reply)
    await matcher.finish(reply)
