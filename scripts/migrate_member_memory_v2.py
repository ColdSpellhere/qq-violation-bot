from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
load_dotenv(PROJECT_DIR / ".env")

from plugins.member_memory.store import migrate_legacy_memory, pending_summary_batch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate legacy member memories to the permanent ledger.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("--database", type=Path, default=PROJECT_DIR / "data" / "chat_archive.db")
    parser.add_argument("--mirror-root", type=Path, default=PROJECT_DIR / "data" / "member_memory")
    args = parser.parse_args()
    if args.summarize and not args.apply:
        parser.error("--summarize requires --apply")
    return args


async def summarize_pending_members(database: Path, mirror_root: Path) -> tuple[int, int]:
    try:
        with sqlite3.connect(database) as conn:
            members = conn.execute("SELECT group_id,user_id FROM member_memories").fetchall()
    except sqlite3.Error:
        return 0, 0
    succeeded = 0
    failed = 0
    for group_id, user_id in members:
        if pending_summary_batch(database, group_id=int(group_id), user_id=str(user_id)) is None:
            continue
        from plugins.member_memory.summary import refresh_member_summary
        try:
            if await refresh_member_summary(
                database, mirror_root, group_id=int(group_id), user_id=str(user_id)
            ):
                succeeded += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return succeeded, failed


def main() -> int:
    args = parse_args()
    report = migrate_legacy_memory(args.database, args.mirror_root, apply=args.apply)
    summary_success = 0
    summary_failed = 0
    if args.summarize:
        summary_success, summary_failed = asyncio.run(
            summarize_pending_members(args.database, args.mirror_root)
        )
    print(
        f"profiles={report.profiles} source_facts={report.source_facts} "
        f"source_aliases={report.source_aliases} inserted_facts={report.inserted_facts} "
        f"inserted_aliases={report.inserted_aliases} summary_success={summary_success} "
        f"summary_failed={summary_failed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
