import asyncio
from datetime import datetime
from pathlib import Path

from nonebot import get_bot, get_bots, get_driver, logger

from .config import CONFIG
from .db import backup_database, init_db
from .exporter import weekly_report
from .service import automatic_maintenance


async def _send_group(text: str) -> None:
    try:
        bot = get_bot()
        for group_id in CONFIG.allowed_group_ids:
            await bot.send_group_msg(group_id=group_id, message=text)
    except Exception as exc:
        logger.warning(f"群通知发送失败：{exc}")


async def _send_group_file(path: Path) -> None:
    try:
        bot = get_bot()
        for group_id in CONFIG.allowed_group_ids:
            await bot.call_api(
                "upload_group_file",
                group_id=str(group_id),
                file=str(path),
                name=path.name,
            )
            await bot.send_group_msg(group_id=group_id, message=f"文件已上传：{path.name}")
    except Exception as exc:
        logger.warning(f"群文件上传失败：{path}: {exc}")
        await _send_group(f"文件上传失败，可从服务器路径下载：{path}\n原因：{exc}")


async def _maintenance_loop() -> None:
    last_backup_day = None
    last_weekly = None
    while True:
        try:
            if get_bots():
                for message in automatic_maintenance():
                    await _send_group(message)
            else:
                logger.debug("Napcat 尚未连接，跳过本轮自动减除/状态维护。")
            now = datetime.now()
            if now.weekday() in {0, 3, 6} and last_backup_day != now.date():
                path = backup_database("scheduled")
                last_backup_day = now.date()
                if path:
                    logger.info(f"数据库备份完成：{path}")
            if now.weekday() == 6 and now.hour == 0 and now.minute >= 10 and last_weekly != now.date():
                path = weekly_report("xlsx")
                last_weekly = now.date()
                await _send_group(f"周报已生成：{path}")
                await _send_group_file(path)
        except Exception as exc:
            logger.exception(f"后台任务失败：{exc}")
        await asyncio.sleep(60)


def setup_scheduler() -> None:
    driver = get_driver()

    @driver.on_startup
    async def _startup() -> None:
        init_db()
        asyncio.create_task(_maintenance_loop())
