from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .rules import MAX_RULES, KeywordRule, normalize_literal_text


@dataclass(frozen=True)
class KeywordMatch:
    rule_id: str
    pattern: str
    start: int
    end: int


class LiteralKeywordMatcher:
    """Bounded Unicode-normalized literal matcher.

    Overlapping candidates are resolved by longest match first.  The final
    result is ordered by message position and contains each rule at most once.
    Regex syntax has no special meaning.
    """

    def __init__(self, rules: Iterable[KeywordRule]):
        snapshot = tuple(rules)
        if len(snapshot) > MAX_RULES:
            raise ValueError(f"keyword rule limit is {MAX_RULES}")

        prepared: list[tuple[KeywordRule, str]] = []
        ids: set[str] = set()
        normalized_patterns: set[str] = set()
        for rule in snapshot:
            if not isinstance(rule, KeywordRule):
                raise TypeError("rules must contain KeywordRule objects")
            normalized = normalize_literal_text(rule.pattern)
            if rule.rule_id in ids:
                raise ValueError(f"duplicate keyword rule id: {rule.rule_id}")
            if normalized in normalized_patterns:
                raise ValueError("duplicate normalized keyword pattern")
            ids.add(rule.rule_id)
            normalized_patterns.add(normalized)
            prepared.append((rule, normalized))
        self._rules = tuple(prepared)

    def match_text(self, text: str) -> tuple[KeywordMatch, ...]:
        normalized_text = normalize_literal_text(text)
        if not normalized_text or not self._rules:
            return ()

        candidates: list[KeywordMatch] = []
        for rule, normalized_pattern in self._rules:
            offset = normalized_text.find(normalized_pattern)
            while offset >= 0:
                candidates.append(
                    KeywordMatch(
                        rule_id=rule.rule_id,
                        pattern=rule.pattern,
                        start=offset,
                        end=offset + len(normalized_pattern),
                    )
                )
                offset = normalized_text.find(normalized_pattern, offset + 1)

        # Select longer candidates first across the whole overlap component.
        # This avoids an earlier short rule shadowing a longer rule that starts
        # one character later.  Stable rule-id ordering makes ties deterministic.
        accepted: list[KeywordMatch] = []
        for candidate in sorted(
            candidates,
            key=lambda item: (-(item.end - item.start), item.start, item.rule_id),
        ):
            if any(_overlaps(candidate, existing) for existing in accepted):
                continue
            accepted.append(candidate)

        accepted.sort(
            key=lambda item: (item.start, -(item.end - item.start), item.rule_id)
        )
        unique: list[KeywordMatch] = []
        seen_rule_ids: set[str] = set()
        for match in accepted:
            if match.rule_id in seen_rule_ids:
                continue
            seen_rule_ids.add(match.rule_id)
            unique.append(match)
        return tuple(unique)


def match_message_text_segments(
    message: object,
    matcher: LiteralKeywordMatcher,
) -> tuple[KeywordMatch, ...]:
    """Scan every text segment separately without joining CQ boundaries."""

    matches: list[KeywordMatch] = []
    seen_rule_ids: set[str] = set()
    for segment in message:  # type: ignore[union-attr]
        if getattr(segment, "type", None) != "text":
            continue
        data = getattr(segment, "data", None)
        if not isinstance(data, dict):
            continue
        value = data.get("text")
        if not isinstance(value, str):
            continue
        for match in matcher.match_text(value):
            if match.rule_id in seen_rule_ids:
                continue
            seen_rule_ids.add(match.rule_id)
            matches.append(match)
    return tuple(matches)


def _overlaps(left: KeywordMatch, right: KeywordMatch) -> bool:
    return left.start < right.end and right.start < left.end
