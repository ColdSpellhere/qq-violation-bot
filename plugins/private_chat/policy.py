def is_private_candidate(
    enabled: bool,
    allowed_user_id: str,
    user_id: str,
    self_id: str,
) -> bool:
    allowed = str(allowed_user_id).strip()
    return (
        enabled
        and allowed.isdigit()
        and str(user_id) == allowed
        and str(user_id) != str(self_id)
    )


def eligible_private_text(text: str) -> str | None:
    cleaned = text.strip()
    if not cleaned or cleaned.startswith("/"):
        return None
    return cleaned
