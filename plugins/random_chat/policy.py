import random


_DIRECT_ATTACK_TERMS = (
    "傻逼",
    "傻屄",
    "煞笔",
    "废物",
    "垃圾",
    "去死",
    "滚开",
    "蠢货",
    "贱人",
    "脑残",
    "死妈",
    "没用的东西",
    "是猪",
    "像猪",
    "头猪",
    "蠢猪",
    "死猪",
    "猪头",
    "是豬",
    "像豬",
    "頭豬",
    "蠢豬",
    "死豬",
    "豬頭",
)

_ALIAS_ATTACK_TERMS = (
    "傻逼",
    "傻屄",
    "煞笔",
    "廢物",
    "废物",
    "垃圾",
    "去死",
    "蠢货",
    "蠢貨",
    "贱人",
    "賤人",
    "脑残",
    "腦殘",
    "死妈",
    "死媽",
    "猪",
    "豬",
)

_NEGATION_MARKERS = ("不", "没", "沒", "无", "無", "别", "別")
_REPORTED_SPEECH_MARKERS = ("说", "說", "骂", "罵", "欺负", "欺負")


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


def is_protected_member_attack(
    text: str,
    *,
    sender_user_id: str,
    at_user_ids: tuple[str, ...],
    protected_user_ids: tuple[str, ...],
    protected_aliases: tuple[str, ...] = (),
) -> bool:
    cleaned = text.strip()
    protected = frozenset(str(value).strip() for value in protected_user_ids)
    sender = str(sender_user_id).strip()
    if not cleaned or cleaned.startswith("/") or not protected or sender in protected:
        return False
    folded = cleaned.casefold()
    if protected.intersection(str(value).strip() for value in at_user_ids):
        return any(term in folded for term in _DIRECT_ATTACK_TERMS)
    for raw_alias in protected_aliases:
        alias = raw_alias.strip().casefold()
        if not alias:
            continue
        offset = 0
        while (position := folded.find(alias, offset)) >= 0:
            prefix = folded[max(0, position - 4) : position]
            if not any(marker in prefix for marker in _REPORTED_SPEECH_MARKERS):
                tail = folded[position + len(alias) : position + len(alias) + 12]
                for term in _ALIAS_ATTACK_TERMS:
                    term_position = tail.find(term)
                    if term_position < 0:
                        continue
                    between = tail[:term_position]
                    if not any(marker in between for marker in _NEGATION_MARKERS):
                        return True
            offset = position + len(alias)
    return False
