"""Public LLM Gateway access without importing the lifecycle entrypoint."""


async def get_gateway(*, driver=None):
    from .runtime import get_gateway as _get_gateway

    return await _get_gateway(driver=driver)


def setup_lifecycle(**kwargs) -> None:
    from .runtime import setup_lifecycle as _setup_lifecycle

    _setup_lifecycle(**kwargs)


__all__ = ["get_gateway", "setup_lifecycle"]
