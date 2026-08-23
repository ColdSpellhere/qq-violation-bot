from .config import CONFIG
from . import matcher as matcher

if CONFIG.business_capable:
    from .scheduler import setup_scheduler

    try:
        setup_scheduler()
    except ValueError:
        # Utility scripts import this package without nonebot.init().
        pass
