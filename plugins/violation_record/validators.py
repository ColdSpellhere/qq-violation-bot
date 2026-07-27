import re
from datetime import datetime, timedelta

from .config import ALL_STATUSES, GROUP_AREAS


def normalize_area(text: str | None) -> str | None:
    if text in GROUP_AREAS:
        return text
    return None


CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "俩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _parse_cn_int(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in CN_DIGITS:
        return CN_DIGITS[text]
    total = 0
    number = 0
    matched = False
    for char in text:
        if char in CN_DIGITS:
            number = CN_DIGITS[char]
            matched = True
        elif char == "十":
            total += (number or 1) * 10
            number = 0
            matched = True
        elif char == "百":
            total += (number or 1) * 100
            number = 0
            matched = True
        else:
            return None
    if not matched:
        return None
    return total + number


def _parse_amount(text: str) -> float | None:
    text = text.strip().removeprefix("个")
    if text in {"几", "数", "若干"}:
        return None
    if text == "半":
        return 0.5
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    if text.endswith("半") and len(text) > 1:
        base = _parse_cn_int(text[:-1])
        return base + 0.5 if base is not None else None
    parsed = _parse_cn_int(text)
    return float(parsed) if parsed is not None else None


def _format(dt: datetime) -> str:
    return dt.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _parse_time_of_day(text: str, base: datetime) -> tuple[int, int] | None:
    text = text.strip()
    if not text:
        return None
    text = text.replace("：", ":").replace("零", "0")
    period = ""
    for marker in ("凌晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "今晚", "半夜"):
        if marker in text:
            period = marker
            text = text.replace(marker, "")
            break
    colon = re.search(r"(?P<h>\d{1,2})\s*:\s*(?P<m>\d{1,2})", text)
    if colon:
        hour = int(colon.group("h"))
        minute = int(colon.group("m"))
    else:
        match = re.search(
            r"(?P<h>\d{1,2}|[一二两俩三四五六七八九十]{1,3})\s*(?:点|时)(?P<half>半)?(?:(?P<m>\d{1,2}|[一二两俩三四五六七八九十]{1,3})\s*分?)?",
            text,
        )
        if not match:
            if period == "中午":
                return 12, 0
            if period == "半夜":
                return 0, 0
            return None
        parsed_hour = _parse_cn_int(match.group("h"))
        parsed_minute = _parse_cn_int(match.group("m")) if match.group("m") else 0
        if parsed_hour is None or parsed_minute is None:
            return None
        hour = parsed_hour
        minute = 30 if match.group("half") else parsed_minute
    if period in {"下午", "傍晚", "晚上", "今晚"} and 0 < hour < 12:
        hour += 12
    if period == "中午" and 0 < hour < 11:
        hour += 12
    if period == "半夜" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _parse_relative_time(text: str, base: datetime) -> str | None:
    compact = re.sub(r"\s+", "", text)
    if compact in {"现在", "当前", "此刻", "刚刚", "刚才", "方才"}:
        return _format(base)
    if "一刻钟前" in compact:
        return _format(base - timedelta(minutes=15))
    if "半小时前" in compact or "半个小时前" in compact or "半小时以前" in compact:
        return _format(base - timedelta(minutes=30))

    if compact.endswith(("前", "以前", "之前")):
        core = re.sub(r"(以前|之前|前)$", "", compact)
        token_re = re.compile(r"(?P<n>\d+(?:\.\d+)?|[零〇一二两俩三四五六七八九十百半几数若干]+)(?:个)?(?P<u>分钟|分|小时|钟头|天|日|周|星期|礼拜)")
        matches = list(token_re.finditer(core))
        if matches and "".join(m.group(0) for m in matches) == core:
            delta = timedelta()
            for match in matches:
                amount = _parse_amount(match.group("n"))
                if amount is None:
                    return None
                unit = match.group("u")
                if unit in {"分钟", "分"}:
                    delta += timedelta(minutes=amount)
                elif unit in {"小时", "钟头"}:
                    delta += timedelta(hours=amount)
                elif unit in {"天", "日"}:
                    delta += timedelta(days=amount)
                elif unit in {"周", "星期", "礼拜"}:
                    delta += timedelta(days=amount * 7)
            return _format(base - delta)

    day_offsets = {
        "今天": 0,
        "今晚": 0,
        "昨天": -1,
        "昨日": -1,
        "前天": -2,
        "大前天": -3,
    }
    for marker, offset in sorted(day_offsets.items(), key=lambda item: len(item[0]), reverse=True):
        if marker in compact:
            rest = compact.replace(marker, "", 1)
            if marker == "今晚":
                rest = f"晚上{rest}"
            tod = _parse_time_of_day(rest or marker, base)
            if tod is None:
                return None
            hour, minute = tod
            return _format((base + timedelta(days=offset)).replace(hour=hour, minute=minute))

    if not re.search(r"(\d{1,4}[/-]\d{1,2}|年|月|日|号)", compact):
        tod = _parse_time_of_day(compact, base)
        if tod is not None:
            hour, minute = tod
            return _format(base.replace(hour=hour, minute=minute))
    return None


def normalize_time(value: str | None, base: datetime | None = None) -> str | None:
    if not value:
        return None
    base = base or datetime.now()
    text = str(value).strip()
    relative = _parse_relative_time(text, base)
    if relative:
        return relative

    date_text = text
    replacements = {
        "年": "/",
        "月": "/",
        "日": " ",
        "号": " ",
        "：": ":",
        "-": "/",
    }
    for src, dst in replacements.items():
        date_text = date_text.replace(src, dst)
    date_text = " ".join(date_text.split())
    date_match = re.match(r"^(?P<date>\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2})(?:\s+(?P<time>.+))?$", date_text)
    if date_match and date_match.group("time"):
        date_part = date_match.group("date")
        time_part = date_match.group("time")
        tod = _parse_time_of_day(time_part, base)
        if tod is not None:
            fmt = "%Y/%m/%d" if date_part.count("/") == 2 and len(date_part.split("/")[0]) == 4 else "%m/%d"
            try:
                dt = datetime.strptime(date_part, fmt)
                if fmt == "%m/%d":
                    dt = dt.replace(year=base.year)
                hour, minute = tod
                return _format(dt.replace(hour=hour, minute=minute))
            except ValueError:
                pass

    text = date_text.replace("点", ":00")
    formats = [
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H",
        "%m/%d %H:%M",
        "%m/%d %H",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=datetime.now().year)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def display_time(value: str | None) -> str:
    if not value:
        return "未知时间"
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%Y/%-m/%-d %H:%M")
    except ValueError:
        return value


def normalize_duration_seconds(value: object, allow_bare_number: bool = True) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = int(float(value))
        return seconds if seconds > 0 else None

    text = str(value).strip()
    if not text:
        return None
    compact = re.sub(r"\s+", "", text)
    compact = compact.replace("半个", "半").replace("个半", "半")
    if allow_bare_number and re.fullmatch(r"\d+(?:\.\d+)?", compact):
        seconds = int(float(compact))
        return seconds if seconds > 0 else None

    unit_seconds = {
        "秒钟": 1,
        "秒": 1,
        "分钟": 60,
        "分": 60,
        "小时": 3600,
        "钟头": 3600,
        "天": 86400,
        "日": 86400,
        "周": 7 * 86400,
        "星期": 7 * 86400,
        "礼拜": 7 * 86400,
        "个月": 30 * 86400,
        "月": 30 * 86400,
    }
    token_re = re.compile(
        r"(?P<n>\d+(?:\.\d+)?|[零〇一二两俩三四五六七八九十百半]+)(?:个)?"
        r"(?P<u>秒钟|秒|分钟|分|小时|钟头|天|日|周|星期|礼拜|个月|月)"
    )
    total = 0.0
    for match in token_re.finditer(compact):
        amount = _parse_amount(match.group("n"))
        if amount is None:
            continue
        total += amount * unit_seconds[match.group("u")]
    if total > 0:
        return int(total)

    english_re = re.compile(
        r"(?P<n>\d+(?:\.\d+)?)(?P<u>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)",
        re.IGNORECASE,
    )
    for match in english_re.finditer(compact):
        amount = float(match.group("n"))
        unit = match.group("u").lower()
        if unit.startswith("s"):
            total += amount
        elif unit in {"m", "min", "mins", "minute", "minutes"}:
            total += amount * 60
        elif unit in {"h", "hr", "hrs", "hour", "hours"}:
            total += amount * 3600
        elif unit in {"d", "day", "days"}:
            total += amount * 86400
    if total > 0:
        return int(total)
    return None


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds == 0:
        return "0秒"
    units = ((86400, "天"), (3600, "小时"), (60, "分钟"), (1, "秒"))
    parts: list[str] = []
    remaining = seconds
    for unit_seconds, label in units:
        value, remaining = divmod(remaining, unit_seconds)
        if value:
            parts.append(f"{value}{label}")
    return "".join(parts)


def normalize_action(action: str | None) -> str | None:
    if not action:
        return None
    text = action.strip()
    if text == "禁言":
        return "禁言10分钟"
    return text


def is_countable_action(action: str | None) -> bool:
    return "警告" not in (action or "")


def normalize_status(status: str | None) -> str | None:
    if status in ALL_STATUSES:
        return status
    if status in {"退群", "已退"}:
        return "已退群"
    if status in {"移出", "踢出", "踢了"}:
        return "已移出"
    if status in {"拉黑", "黑名单"}:
        return "已拉黑"
    return None


def first_missing(fields: list[str]) -> str:
    mapping = {
        "group_area": "群聊：蜂巢 / 蜂窝 / 蜂箱",
        "target": "违规者 QQ号 或 QQ昵称",
        "target.qq_number": "违规者 QQ号",
        "target.qq_nickname": "违规者 QQ昵称",
        "violation.time": "违规时间",
        "violation.judgement": "判定原因",
        "violation.action": "处理措施",
        "violation.handler_admin_qq": "处理人 QQ号",
        "violation.handler_admin_nickname": "处理人昵称",
    }
    return "、".join(mapping.get(f, f) for f in fields)
