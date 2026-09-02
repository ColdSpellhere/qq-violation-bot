"""Register content-alert handlers only for a fully configured instance."""

from plugins.violation_record.config import CONFIG

if CONFIG.content_alert_enabled and CONFIG.content_alert_capable:
    from . import matcher  # noqa: F401
