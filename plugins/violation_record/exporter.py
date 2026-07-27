import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from .admin_resolver import resolve_operator
from .config import EXPORT_DIR
from .db import connect, now_str


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _range_from_intent(intent: dict[str, Any]) -> tuple[str, str | None, str | None]:
    query = intent.get("query") or {}
    raw = str(intent.get("_raw", ""))
    time_range = query.get("time_range")
    if not time_range:
        if "本月" in raw or "这个月" in raw or "当月" in raw:
            time_range = "current_month"
        elif "本周" in raw or "这周" in raw or "这个星期" in raw:
            time_range = "current_week"
        elif "昨天" in raw or "昨日" in raw:
            time_range = "yesterday"
        elif "今天" in raw or "今日" in raw:
            time_range = "today"
        elif "最近" in raw:
            time_range = "recent_days"
        else:
            time_range = "all"
    now = datetime.now().replace(second=0, microsecond=0)
    if time_range == "today":
        start = now.replace(hour=0, minute=0)
        return "今天", start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    if time_range == "yesterday":
        day = now - timedelta(days=1)
        return "昨天", day.replace(hour=0, minute=0).strftime("%Y-%m-%d %H:%M:%S"), day.replace(hour=23, minute=59).strftime("%Y-%m-%d %H:%M:%S")
    if time_range == "current_week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0)
        return "本周", start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    if time_range == "current_month":
        start = now.replace(day=1, hour=0, minute=0)
        return "本月", start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    if time_range == "recent_days":
        days = int(query.get("recent_days") or 14)
        start = now - timedelta(days=days)
        return f"最近{days}天", start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    return "全部", None, None


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _write_xlsx(path: Path, rows: list[dict[str, Any]]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "data"
    if rows:
        headers = list(rows[0].keys())
        ws.append(headers)
        for row in rows:
            ws.append([row.get(h) for h in headers])
    wb.save(path)


def export_records(area: str | None = None, fmt: str = "xlsx", range_label: str = "全部", start: str | None = None, end: str | None = None) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        where = ["v.is_withdrawn=0"]
        params: list[Any] = []
        if area:
            where.append("v.group_area=?")
            params.append(area)
        if start and end:
            where.append("v.violation_time BETWEEN ? AND ?")
            params.extend([start, end])
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        rows = [dict(r) for r in conn.execute(
            f"""
            SELECT
                v.group_area AS 群聊,
                m.qq_nickname AS QQ昵称,
                m.qq_number AS QQ号,
                v.violation_time AS 违规时间,
                v.judgement AS 判定,
                v.action AS 处理措施,
                COALESCE(handler.nickname, '') AS 处理人,
                COALESCE(recorder.nickname, '') AS 记录人,
                COALESCE(v.remark, '无') AS 备注
            FROM violation_records v
            JOIN members m ON m.id=v.member_id
            LEFT JOIN admins handler ON handler.id=v.handler_admin_id
            LEFT JOIN admins recorder ON recorder.id=v.recorder_admin_id
            {where_sql}
            ORDER BY v.violation_time DESC
            """,
            params,
        ).fetchall()]
    range_part = "" if range_label == "全部" else f"_{range_label}"
    path = EXPORT_DIR / f"{area or '全部'}{range_part}_violation_records_{_stamp()}.{fmt}"
    _write_xlsx(path, rows) if fmt == "xlsx" else _write_csv(path, rows)
    return path


def export_logs(area: str | None = None, fmt: str = "xlsx") -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        if area:
            rows = [dict(r) for r in conn.execute("SELECT * FROM operation_logs WHERE group_area=? ORDER BY created_at DESC", (area,)).fetchall()]
        else:
            rows = [dict(r) for r in conn.execute("SELECT * FROM operation_logs ORDER BY created_at DESC").fetchall()]
    path = EXPORT_DIR / f"{area or '全部'}_operation_logs_{_stamp()}.{fmt}"
    _write_xlsx(path, rows) if fmt == "xlsx" else _write_csv(path, rows)
    return path


def weekly_report(fmt: str = "xlsx") -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        updates = [dict(r) for r in conn.execute("SELECT * FROM operation_logs WHERE created_at>=? ORDER BY created_at", (since,)).fetchall()]
        stats = [dict(r) for r in conn.execute(
            """
            SELECT s.group_area, m.qq_nickname, m.qq_number, s.status, s.total_count, s.deduct_count, s.current_count_cache
            FROM member_group_states s JOIN members m ON m.id=s.member_id
            ORDER BY s.group_area, s.current_count_cache DESC
            """
        ).fetchall()]
    path = EXPORT_DIR / f"weekly_report_{_stamp()}.{fmt}"
    if fmt == "xlsx":
        wb = Workbook()
        ws = wb.active
        ws.title = "本周更新"
        if updates:
            headers = list(updates[0].keys())
            ws.append(headers)
            for row in updates:
                ws.append([row.get(h) for h in headers])
        ws2 = wb.create_sheet("当前统计")
        if stats:
            headers = list(stats[0].keys())
            ws2.append(headers)
            for row in stats:
                ws2.append([row.get(h) for h in headers])
        wb.save(path)
    else:
        _write_csv(path, updates)
    return path


def export_by_intent(intent: dict[str, Any], operator_qq: str, operator_nickname: str | None, message_id: str | None) -> str:
    area = intent.get("group_area")
    raw = intent.get("_raw", "")
    fmt = "csv" if "csv" in raw.lower() else "xlsx"
    range_label, start, end = _range_from_intent(intent)
    try:
        if "日志" in raw:
            path = export_logs(area, fmt)
            op_type = "导出日志"
        else:
            path = export_records(area, fmt, range_label, start, end)
            op_type = "导出"
        with connect() as conn:
            operator = resolve_operator(operator_qq, operator_nickname)
            conn.execute(
                """
                INSERT INTO operation_logs(group_area, operation_type, source, operator_qq, operator_nickname,
                    target_member_id, before_json, after_json, message_id, created_at, remark)
                VALUES(?, ?, '手动', ?, ?, NULL, NULL, ?, ?, ?, ?)
                """,
                (area, op_type, (operator or {}).get("qq_number"), (operator or {}).get("nickname"), str(path), message_id, now_str(), f"导出文件；范围={range_label}"),
            )
        return f"已导出：{path}"
    except Exception as exc:
        return f"导出失败：{exc}"
