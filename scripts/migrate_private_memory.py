from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.private_memory.models import MigrationReport
from plugins.private_memory.schema import (
    migrate,
    online_backup,
    prune_private_memory_backups,
    quick_check,
    require_regular_database,
    schema_version,
    validate_backup_directory,
)
from plugins.violation_record.config import CONFIG


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate the private-memory chat archive schema.")
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data" / "chat_archive.db")
    parser.add_argument("--backup-dir", type=Path, default=PROJECT_ROOT / "backups")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def apply_migration(database: Path, backup_dir: Path) -> tuple[MigrationReport, Path]:
    database = Path(database)
    backup_dir = Path(backup_dir)
    require_regular_database(database)
    validate_backup_directory(backup_dir)
    quick_check(database)
    now = datetime.now(timezone.utc)
    prune_private_memory_backups(
        backup_dir,
        now=now,
        retention_days=CONFIG.private_memory_retention_days,
    )
    timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    backup = online_backup(database, backup_dir / f"chat_archive_before_private_memory_{timestamp}.sqlite3")
    quick_check(backup)
    report = migrate(database)
    quick_check(database)
    return report, backup


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        require_regular_database(args.database)
        validate_backup_directory(args.backup_dir)
        result = quick_check(args.database)
        current_version = schema_version(args.database)
        if not args.apply:
            print(
                f"preflight=ok quick_check={result} schema_version={current_version} "
                f"database={args.database}"
            )
            return 0
        report, backup = apply_migration(args.database, args.backup_dir)
        print(
            f"migration=ok schema_version={report.schema_version} "
            f"tables_created={report.tables_created} columns_added={report.columns_added} "
            f"backup={backup}"
        )
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"migration=failed error={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
