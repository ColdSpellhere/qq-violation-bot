#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from plugins.violation_record.db import connect, init_db, now_str


def load_aliases(raw: str | None) -> list[str]:
    try:
        aliases = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(aliases, list):
        return []
    cleaned: list[str] = []
    for item in aliases:
        alias = str(item).strip()
        if alias and alias not in cleaned:
            cleaned.append(alias)
    return cleaned


def merge_aliases(raw: str | None, explicit_aliases: list[str], old_nickname: str | None, new_nickname: str) -> str:
    aliases = load_aliases(raw)
    for item in [old_nickname, *explicit_aliases]:
        alias = (item or "").strip()
        if not alias or alias == new_nickname or alias in aliases:
            continue
        aliases.append(alias)
    return json.dumps(aliases, ensure_ascii=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="维护 QQ 违规记录机器人管理员")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("qq_number")
    add.add_argument("nickname")
    add.add_argument("--aliases", default="", help="用逗号分隔")
    sub.add_parser("list")
    args = parser.parse_args()
    init_db()
    if args.cmd == "add":
        aliases = [x.strip() for x in args.aliases.split(",") if x.strip()]
        ts = now_str()
        with connect() as conn:
            existing = conn.execute("SELECT * FROM admins WHERE qq_number=?", (args.qq_number,)).fetchone()
            merged_aliases = merge_aliases(existing["aliases"] if existing else None, aliases, existing["nickname"] if existing else None, args.nickname)
            if existing:
                conn.execute(
                    """
                    UPDATE admins
                    SET nickname=?, aliases=?, is_active=1, updated_at=?
                    WHERE qq_number=?
                    """,
                    (args.nickname, merged_aliases, ts, args.qq_number),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO admins(qq_number, nickname, aliases, is_active, created_at, updated_at)
                    VALUES(?, ?, ?, 1, ?, ?)
                    """,
                    (args.qq_number, args.nickname, merged_aliases, ts, ts),
                )
        print("ok")
    elif args.cmd == "list":
        with connect() as conn:
            for row in conn.execute("SELECT qq_number, nickname, aliases, is_active FROM admins ORDER BY id").fetchall():
                print(dict(row))


if __name__ == "__main__":
    main()
