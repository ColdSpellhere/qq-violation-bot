from . import matcher as matcher
from .scheduler import setup_scheduler

try:
    setup_scheduler()
except ValueError:
    # Utility scripts import this package without nonebot.init().
    pass
