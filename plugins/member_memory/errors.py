"""Content-free diagnostics; existing queue retry policy stays unchanged."""


class MemberMemoryError(RuntimeError):
    code = "member_memory_processing_error"
    retryable = True


class MemberSummaryError(MemberMemoryError):
    _CODES = frozenset({
        "member_summary_configuration_error",
        "member_summary_request_timeout",
        "member_summary_transport_error",
        "member_summary_rate_limited",
        "member_summary_server_error",
        "member_summary_auth_error",
        "member_summary_payment_required",
        "member_summary_client_error",
        "member_summary_invalid_response",
        "member_summary_empty_response",
        "member_summary_too_long",
        "member_summary_secret_blocked",
        "member_summary_cursor_conflict",
        "member_summary_fact_conflict",
        "member_summary_storage_error",
        "member_summary_generation_failed",
    })

    def __init__(self, code: str):
        if code not in self._CODES:
            raise ValueError("unknown member summary error code")
        self.code = code
        super().__init__(code)
