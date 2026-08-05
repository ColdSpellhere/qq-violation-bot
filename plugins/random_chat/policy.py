import random


def is_candidate(
    enabled: bool,
    target_group_id: int,
    group_id: int,
    user_id: int,
    self_id: int,
) -> bool:
    return enabled and group_id == target_group_id and user_id != self_id


def eligible_text(text: str, *, at_bot: bool) -> str | None:
    cleaned = text.strip()
    if not cleaned or at_bot or cleaned.startswith("/"):
        return None
    return cleaned


def should_reply(probability: float, *, sample: float | None = None) -> bool:
    bounded = min(1.0, max(0.0, probability))
    value = random.random() if sample is None else sample
    return bounded > 0.0 and value < bounded
