def is_private_candidate(
    enabled: bool,
    allowed_user_id: str,
    user_id: str,
    self_id: str,
) -> bool:
    allowed = {
        item.strip()
        for item in str(allowed_user_id).split(",")
        if item.strip().isdigit()
    }
    sender = str(user_id)
    return (
        enabled
        and sender in allowed
        and sender != str(self_id)
    )


def eligible_private_text(text: str) -> str | None:
    cleaned = text.strip()
    if not cleaned or cleaned.startswith("/"):
        return None
    return cleaned
