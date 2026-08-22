from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from .contracts import GatewayCompletion, GatewayRequest
from .errors import GatewayError
from plugins.private_memory.schema import PRIVATE_MEMORY_SCHEMA_VERSION


_REQUIRED_COLUMNS = {
    "id",
    "task",
    "model",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_microunits",
    "cost_currency",
    "latency_ms",
    "status",
    "retry_count",
    "error_class",
    "created_at",
}


class UsageStore:
    def __init__(self, path: Path, *, logger: logging.Logger | None = None) -> None:
        self._path = Path(path)
        self._logger = logger or logging.getLogger(__name__)
        self._validate_schema()

    def _validate_schema(self) -> None:
        if not self._path.is_file():
            raise RuntimeError("usage schema is unavailable")
        try:
            with closing(
                sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
            ) as connection:
                row = connection.execute(
                    "SELECT schema_version FROM private_memory_schema_meta WHERE singleton=1"
                ).fetchone()
                columns = {
                    str(item[1])
                    for item in connection.execute("PRAGMA table_info(llm_usage_events)")
                }
        except sqlite3.Error as exc:
            raise RuntimeError("usage schema is unavailable") from exc
        if row is None or int(row[0]) != PRIVATE_MEMORY_SCHEMA_VERSION:
            raise RuntimeError("usage schema version is unsupported")
        if columns != _REQUIRED_COLUMNS:
            raise RuntimeError("usage schema is incomplete")

    def record_success(
        self, request: GatewayRequest, completion: GatewayCompletion
    ) -> None:
        self._record(
            task=request.task.value,
            model=completion.model,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            total_tokens=completion.usage.total_tokens,
            latency_ms=completion.latency_ms,
            status="success",
            retries=completion.retries,
            error_class=None,
        )

    def record_failure(
        self,
        request: GatewayRequest,
        *,
        latency_ms: int,
        retries: int,
        error: GatewayError,
    ) -> None:
        self._record(
            task=request.task.value,
            model=request.model,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            latency_ms=latency_ms,
            status="failure",
            retries=retries,
            error_class=type(error).__name__,
        )

    def _record(
        self,
        *,
        task: str,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
        latency_ms: int,
        status: str,
        retries: int,
        error_class: str | None,
    ) -> None:
        try:
            with closing(sqlite3.connect(self._path)) as connection:
                connection.execute(
                    """
                    INSERT INTO llm_usage_events(
                        task,model,input_tokens,output_tokens,total_tokens,
                        cost_microunits,cost_currency,latency_ms,status,retry_count,
                        error_class,created_at
                    ) VALUES(?,?,?,?,?,NULL,NULL,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    """,
                    (
                        task,
                        model,
                        input_tokens,
                        output_tokens,
                        total_tokens,
                        latency_ms,
                        status,
                        retries,
                        error_class,
                    ),
                )
                connection.commit()
        except (OSError, sqlite3.Error) as exc:
            self._logger.warning(
                "llm usage write failed error_class=%s", type(exc).__name__
            )
