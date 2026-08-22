"""Register private-memory lifecycle hooks; database work starts at app startup."""

from .lifecycle import set_processor, setup_lifecycle


setup_lifecycle()

__all__ = ["set_processor", "setup_lifecycle"]
