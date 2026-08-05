import random


def eligible_text(text: str, *, at_bot: bool) -> str | None:
    cleaned = text.strip()
    if not cleaned or at_bot or cleaned.startswith("/"):
        return None
    return cleaned


def should_reply(probability: float, *, sample: float | None = None) -> bool:
    bounded = min(1.0, max(0.0, probability))
    value = random.random() if sample is None else sample
    return bounded > 0.0 and value < bounded
