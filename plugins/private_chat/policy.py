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


def eligible_private_text(text: str, *, has_image: bool = False) -> str | None:
    cleaned = text.strip()
    if cleaned.startswith("/"):
        return None
    if cleaned:
        return cleaned
    return "[图片]" if has_image else None
