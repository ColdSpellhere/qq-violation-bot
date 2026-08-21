import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from nonebot import get_bot, get_bots, get_driver, logger

from . import policy_bridge
from .config import CONFIG
from .db import backup_database, connect, init_db
from .evidence_store import EvidenceStore
from .exporter import weekly_report
from .policy_schema import require_v102_ready
from .service import automatic_maintenance


_maintenance_task: asyncio.Task | None = None
_last_backup_day = None
_last_weekly = None
_OUTBOX_LEASE_MINUTES = 5
_OUTBOX_RETRY_MINUTES = 5
_OUTBOX_DELIVERY_BATCH = 10


class _DeferredFeatures:
    def business_allowed(self, group_id: int, target_group_id: int) -> bool:
        from plugins.feature_control.runtime import FEATURES

        return FEATURES.business_allowed(group_id, target_group_id)


FEATURES = _DeferredFeatures()


async def _send_group(text: str) -> None:
    try:
        bot = get_bot()
        await bot.send_group_msg(group_id=CONFIG.target_group_id, message=text)
    except Exception as exc:
        logger.warning(f"群通知发送失败：{exc}")


async def _send_group_file(path: Path) -> None:
    try:
        bot = get_bot()
        await bot.call_api(
            "upload_group_file",
            group_id=str(CONFIG.target_group_id),
            file=str(path),
            name=path.name,
        )
        await bot.send_group_msg(
            group_id=CONFIG.target_group_id, message=f"文件已上传：{path.name}"
        )
    except Exception as exc:
        logger.warning(f"群文件上传失败：{path}: {exc}")
        await _send_group(f"文件上传失败，可从服务器路径下载：{path}\n原因：{exc}")


def _claim_policy_outbox(as_of: str, limit: int) -> list[dict]:
    with connect() as conn:
        lease_cutoff = (
            datetime.fromisoformat(as_of)
            - timedelta(minutes=_OUTBOX_LEASE_MINUTES)
        ).strftime("%Y-%m-%d %H:%M:%S")
        stale_rows = conn.execute(
            """
            SELECT * FROM v102_notification_outbox
            WHERE status='sending' AND updated_at<=?
            ORDER BY updated_at, id
            """,
            (lease_cutoff,),
        ).fetchall()
        for stale in stale_rows:
            attempt_number = int(stale["attempt_count"] or 0)
            if attempt_number > 0:
                conn.execute(
                    """
                    INSERT INTO v102_notification_attempts(
                        outbox_id, attempt_number, status, started_at,
                        finished_at, detail, created_at, updated_at
                    ) VALUES(?, ?, 'lease_expired', ?, ?, ?, ?, ?)
                    ON CONFLICT(outbox_id, attempt_number) DO UPDATE SET
                        status='lease_expired', finished_at=excluded.finished_at,
                        detail=excluded.detail, updated_at=excluded.updated_at
                    """,
                    (
                        stale["id"],
                        attempt_number,
                        stale["updated_at"],
                        as_of,
                        "发送租约超时，已自动回收",
                        stale["updated_at"],
                        as_of,
                    ),
                )
            conn.execute(
                """
                UPDATE v102_notification_outbox
                SET status='failed', last_error='sending lease expired',
                    scheduled_at=?, updated_at=?
                WHERE id=? AND status='sending'
                """,
                (as_of, as_of, stale["id"]),
            )
        rows = conn.execute(
            """
            SELECT * FROM v102_notification_outbox
            WHERE status='pending' AND scheduled_at<=?
            ORDER BY scheduled_at, id LIMIT ?
            """,
            (as_of, limit),
        ).fetchall()
        claimed: list[dict] = []
        for row in rows:
            cursor = conn.execute(
                """
                UPDATE v102_notification_outbox
                SET status='sending', attempt_count=attempt_count+1,
                    updated_at=?
                WHERE id=? AND status='pending'
                """,
                (as_of, row["id"]),
            )
            if cursor.rowcount:
                attempt_number = int(row["attempt_count"] or 0) + 1
                conn.execute(
                    """
                    INSERT INTO v102_notification_attempts(
                        outbox_id, attempt_number, status, started_at,
                        created_at, updated_at
                    ) VALUES(?, ?, 'sending', ?, ?, ?)
                    """,
                    (row["id"], attempt_number, as_of, as_of, as_of),
                )
                claimed_row = dict(row)
                claimed_row["status"] = "sending"
                claimed_row["attempt_count"] = attempt_number
                claimed_row["updated_at"] = as_of
                claimed.append(claimed_row)
        return claimed


def _policy_outbox_valid(
    row: dict, *, expected_status: str
) -> tuple[bool, str]:
    with connect() as conn:
        current = conn.execute(
            "SELECT * FROM v102_notification_outbox WHERE id=?", (row["id"],)
        ).fetchone()
        if current is None or current["status"] != expected_status:
            return False, "通知任务已不处于发送状态"
        event = conn.execute(
            """
            SELECT e.is_effective, e.event_type,
                   cause.event_type AS cause_event_type
            FROM v102_policy_events e
            LEFT JOIN v102_policy_events cause ON cause.id=e.caused_by_event_id
            WHERE e.id=?
            """,
            (row["event_id"],),
        ).fetchone()
        if event is None or not int(event["is_effective"] or 0):
            return False, "来源事件已失效"
        if (
            event["event_type"] == "cycle_started"
            and event["cause_event_type"] == "baseline_migrated"
        ):
            return False, "基线迁移初始化事件只保留审计，不对外通知"
        if row["message_type"] == "pending_reminder":
            if row.get("pending_action_id") is not None:
                pending = conn.execute(
                    """
                    SELECT status, caused_by_event_id
                    FROM v102_pending_actions WHERE id=?
                    """,
                    (row["pending_action_id"],),
                ).fetchone()
            else:
                pending = conn.execute(
                    """
                    SELECT status, caused_by_event_id
                    FROM v102_pending_actions
                    WHERE member_id=? AND group_area=?
                      AND caused_by_event_id=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (row["member_id"], row["group_area"], row["event_id"]),
                ).fetchone()
            if (
                pending is None
                or pending["status"] != "pending"
                or int(pending["caused_by_event_id"] or 0) != int(row["event_id"])
            ):
                return False, "管理待办已处理或取消"
    return True, ""


def _claimed_outbox_valid(row: dict) -> tuple[bool, str]:
    return _policy_outbox_valid(row, expected_status="sending")


def defer_policy_outbox(reason: str, as_of: str) -> int:
    if reason not in {"business_disabled", "bot_offline"}:
        raise ValueError(f"unsupported policy outbox deferral reason: {reason}")
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE v102_notification_outbox
            SET status='failed', last_error=?, updated_at=?
            WHERE status='pending' AND scheduled_at<=?
            """,
            (reason, as_of, as_of),
        )
        return cursor.rowcount


def _missed_policy_node(bot, content: str) -> dict:
    return {
        "type": "node",
        "data": {
            "user_id": str(getattr(bot, "self_id", "0")),
            "nickname": "违规记录机器人",
            "content": content,
        },
    }


def _missed_reason(last_error: str | None) -> str:
    if last_error == "business_disabled":
        return "业务关闭"
    if last_error == "bot_offline":
        return "QQ离线"
    return "发送失败"


async def deliver_missed_policy_summary(
    bot, *, as_of: str | None = None
) -> int:
    moment = as_of or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        selected = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM v102_notification_outbox
                WHERE status='failed' AND scheduled_at<=?
                ORDER BY scheduled_at, id
                """,
                (moment,),
            )
        ]

    valid_rows: list[dict] = []
    seen_ids: set[int] = set()
    for row in selected:
        outbox_id = int(row["id"])
        if outbox_id in seen_ids:
            continue
        seen_ids.add(outbox_id)
        valid, invalid_reason = _policy_outbox_valid(
            row, expected_status="failed"
        )
        if valid:
            valid_rows.append(row)
            continue
        with connect() as conn:
            conn.execute(
                """
                UPDATE v102_notification_outbox
                SET status='cancelled', last_error=?, updated_at=?
                WHERE id=? AND status='failed'
                """,
                (invalid_reason, moment, outbox_id),
            )

    if not valid_rows:
        return 0

    reason_counts = {"业务关闭": 0, "QQ离线": 0, "发送失败": 0}
    for row in valid_rows:
        reason_counts[_missed_reason(row.get("last_error"))] += 1
    first = valid_rows[0]["scheduled_at"]
    last = valid_rows[-1]["scheduled_at"]
    overview = (
        "未发送业务提醒概览\n"
        f"时间范围：{first} 至 {last}\n"
        f"涉及提醒：{len(valid_rows)} 条\n"
        f"原因：业务关闭 {reason_counts['业务关闭']} / "
        f"QQ离线 {reason_counts['QQ离线']} / "
        f"发送失败 {reason_counts['发送失败']}"
    )
    nodes = [_missed_policy_node(bot, overview)]
    nodes.extend(
        _missed_policy_node(
            bot,
            (
                f"未发送提醒 #{row['id']}\n"
                f"时间：{row['scheduled_at']}\n"
                f"原因：{_missed_reason(row.get('last_error'))}\n"
                f"{row['message_text']}"
            ),
        )
        for row in valid_rows
    )
    try:
        await bot.call_api(
            "send_group_forward_msg",
            group_id=CONFIG.target_group_id,
            messages=nodes,
        )
    except Exception as exc:
        logger.warning(
            f"未发送业务提醒汇总失败 error={type(exc).__name__}"
        )
        return 0

    outbox_ids = [int(row["id"]) for row in valid_rows]
    placeholders = ",".join("?" for _ in outbox_ids)
    with connect() as conn:
        cursor = conn.execute(
            f"""
            UPDATE v102_notification_outbox
            SET status='sent', sent_at=?, last_error=NULL, updated_at=?
            WHERE status='failed' AND id IN ({placeholders})
            """,
            (moment, moment, *outbox_ids),
        )
        return cursor.rowcount


def _finish_outbox_attempt(
    conn,
    row: dict,
    *,
    status: str,
    finished_at: str,
    detail: str | None,
) -> None:
    conn.execute(
        """
        UPDATE v102_notification_attempts
        SET status=?, finished_at=?, detail=?, updated_at=?
        WHERE outbox_id=? AND attempt_number=?
        """,
        (
            status,
            finished_at,
            detail,
            finished_at,
            row["id"],
            row["attempt_count"],
        ),
    )


async def deliver_policy_outbox(
    bot, *, as_of: str | None = None, limit: int = 100
) -> int:
    moment = as_of or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = _claim_policy_outbox(moment, limit)
    sent = 0
    for row in rows:
        valid, invalid_reason = _claimed_outbox_valid(row)
        if not valid:
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE v102_notification_outbox
                    SET status='cancelled', last_error=?, updated_at=?
                    WHERE id=? AND status='sending'
                    """,
                    (invalid_reason, moment, row["id"]),
                )
                _finish_outbox_attempt(
                    conn,
                    row,
                    status="cancelled",
                    finished_at=moment,
                    detail=invalid_reason,
                )
            continue
        try:
            await bot.send_group_msg(
                group_id=CONFIG.target_group_id,
                message=row["message_text"],
            )
        except Exception as exc:
            retry_at = (
                datetime.fromisoformat(moment)
                + timedelta(minutes=_OUTBOX_RETRY_MINUTES)
            ).strftime("%Y-%m-%d %H:%M:%S")
            detail = f"{type(exc).__name__}: {exc}"
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE v102_notification_outbox
                    SET status='failed', last_error=?, scheduled_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (detail, retry_at, moment, row["id"]),
                )
                _finish_outbox_attempt(
                    conn,
                    row,
                    status="failed",
                    finished_at=moment,
                    detail=detail,
                )
            logger.warning(
                f"策略通知发送失败 outbox={row['id']} error={type(exc).__name__}"
            )
            continue
        with connect() as conn:
            conn.execute(
                """
                UPDATE v102_notification_outbox
                SET status='sent', sent_at=?, last_error=NULL, updated_at=?
                WHERE id=?
                """,
                (moment, moment, row["id"]),
            )
            _finish_outbox_attempt(
                conn,
                row,
                status="sent",
                finished_at=moment,
                detail=None,
            )
        sent += 1
    return sent


async def maintenance_tick(
    *, now: str | None = None, run_periodic_files: bool = True
) -> None:
    global _last_backup_day, _last_weekly

    moment = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current = datetime.fromisoformat(moment)
    bots = get_bots()

    if CONFIG.deduction_policy_v102_enabled:
        stats = policy_bridge.run_policy_maintenance(moment)
        if any(stats.values()):
            logger.info(f"v102策略维护完成：{stats}")
        if not FEATURES.business_allowed(
            CONFIG.target_group_id, CONFIG.target_group_id
        ):
            defer_policy_outbox("business_disabled", moment)
        elif not bots:
            defer_policy_outbox("bot_offline", moment)
        else:
            bot = next(iter(bots.values()))
            await deliver_missed_policy_summary(bot, as_of=moment)
            await deliver_policy_outbox(
                bot, as_of=moment, limit=_OUTBOX_DELIVERY_BATCH
            )
    elif bots:
        for message in automatic_maintenance():
            await _send_group(message)
    else:
        logger.debug("NapCat 尚未连接，旧版自动维护等待下一轮。")

    if run_periodic_files:
        if current.weekday() in {0, 3, 6} and _last_backup_day != current.date():
            path = backup_database("scheduled")
            _last_backup_day = current.date()
            if path:
                logger.info(f"数据库备份完成：{path}")
        if (
            current.weekday() == 6
            and current.hour == 0
            and current.minute >= 10
            and _last_weekly != current.date()
        ):
            path = weekly_report("xlsx")
            _last_weekly = current.date()
            await _send_group(f"周报已生成：{path}")
            await _send_group_file(path)

    evidence_store = EvidenceStore(
        CONFIG.evidence_database_path, CONFIG.evidence_root
    )
    evidence_store.retry_binding_queue()
    evidence_store.cleanup_transient()


async def _maintenance_loop() -> None:
    while True:
        try:
            await maintenance_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"后台任务失败：{exc}")
        await asyncio.sleep(60)


def setup_scheduler() -> None:
    driver = get_driver()

    @driver.on_startup
    async def _startup() -> None:
        global _maintenance_task
        init_db()
        if CONFIG.deduction_policy_v102_enabled:
            with connect() as conn:
                require_v102_ready(conn)
        if _maintenance_task is None or _maintenance_task.done():
            _maintenance_task = asyncio.create_task(
                _maintenance_loop(), name="violation-policy-maintenance"
            )

    @driver.on_shutdown
    async def _shutdown() -> None:
        global _maintenance_task
        task = _maintenance_task
        _maintenance_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
