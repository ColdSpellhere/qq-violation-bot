from __future__ import annotations

from .contracts import LLMTask


class GatewayError(Exception):
    def __init__(
        self,
        _detail: str = "",
        *,
        task: LLMTask | str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__()
        if task is None:
            self.task = None
        else:
            self.task = LLMTask(task).value
        if status_code is not None and (
            not isinstance(status_code, int) or isinstance(status_code, bool)
        ):
            raise ValueError("status_code must be an integer")
        self.status_code = status_code

    def __str__(self) -> str:
        parts = [type(self).__name__]
        if self.task is not None:
            parts.append(f"task={self.task}")
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        return " ".join(parts)


class GatewayConfigurationError(GatewayError):
    pass


class GatewayAuthenticationError(GatewayError):
    pass


class GatewayTimeout(GatewayError):
    pass


class GatewayTransportError(GatewayError):
    pass


class GatewayRateLimitError(GatewayError):
    pass


class GatewayServerError(GatewayError):
    pass


class GatewayClientError(GatewayError):
    pass


class GatewayContractError(GatewayError):
    pass


class GatewayEmptyContentError(GatewayError):
    pass


_RETRYABLE_ERRORS = (
    GatewayTimeout,
    GatewayTransportError,
    GatewayRateLimitError,
    GatewayServerError,
)


def is_retryable(error: BaseException) -> bool:
    return isinstance(error, _RETRYABLE_ERRORS)
