"""Register the monitor only for an instance with a complete private config."""

from plugins.violation_record.config import CONFIG


if (
    CONFIG.hive_member_monitor_capable
    and CONFIG.hive_member_monitor_enabled
):
    from .lifecycle import setup_lifecycle

    setup_lifecycle()
    from . import matcher as matcher
