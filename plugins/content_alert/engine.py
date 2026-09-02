from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .rules import MAX_RULES, KeywordRule, normalize_literal_text


@dataclass(frozen=True)
class KeywordMatch:
    rule_id: str
    pattern: str
    start: int
    end: int


@dataclass(frozen=True)
class ScalableLiteralMatch:
    """One match produced by :class:`ScalableLiteralMatcher`."""

    key: str
    pattern: str
    start: int
    end: int


class ScalableLiteralMatcher:
    """Compile a large, immutable set of normalized literals into a trie.

    The operator-authored matcher above deliberately retains its 200-rule
    limit.  Managed catalogs use this separate matcher so raising their limit
    cannot accidentally widen the QQ command surface.
    """

    def __init__(
        self,
        patterns: Iterable[tuple[str, str]],
        *,
        max_patterns: int,
        max_nodes: int | None = None,
        overlap_groups: Mapping[str, str] | None = None,
    ) -> None:
        if (
            isinstance(max_patterns, bool)
            or not isinstance(max_patterns, int)
            or max_patterns <= 0
        ):
            raise ValueError("max_patterns must be a positive integer")
        if max_nodes is not None and (
            isinstance(max_nodes, bool)
            or not isinstance(max_nodes, int)
            or max_nodes <= 0
        ):
            raise ValueError("max_nodes must be a positive integer")

        # Each node maps one normalized character to the next node index.
        # Terminals contain exactly one key because normalized patterns are
        # required to be unique before the trie is published.
        transitions: list[dict[str, int]] = [{}]
        terminals: list[tuple[str, str] | None] = [None]
        seen_keys: set[str] = set()
        seen_patterns: set[str] = set()
        for count, (key, pattern) in enumerate(patterns, start=1):
            if count > max_patterns:
                raise ValueError("managed literal pattern limit exceeded")
            if not isinstance(key, str) or not key:
                raise ValueError("managed literal key must be a non-empty string")
            if key in seen_keys:
                raise ValueError("duplicate managed literal key")
            if not isinstance(pattern, str):
                raise TypeError("managed literal pattern must be a string")
            normalized = normalize_literal_text(pattern)
            if not normalized:
                raise ValueError("managed literal pattern must not be empty")
            if normalized in seen_patterns:
                raise ValueError("duplicate normalized managed literal pattern")

            seen_keys.add(key)
            seen_patterns.add(normalized)
            node_index = 0
            for character in normalized:
                next_index = transitions[node_index].get(character)
                if next_index is None:
                    if max_nodes is not None and len(transitions) >= max_nodes:
                        raise ValueError("managed literal trie node limit exceeded")
                    next_index = len(transitions)
                    transitions[node_index][character] = next_index
                    transitions.append({})
                    terminals.append(None)
                node_index = next_index
            terminals[node_index] = (key, pattern)

        if overlap_groups is None:
            self._overlap_groups = MappingProxyType({})
        else:
            if set(overlap_groups) != seen_keys or any(
                not isinstance(group, str) or not group
                for group in overlap_groups.values()
            ):
                raise ValueError("managed literal overlap groups are invalid")
            self._overlap_groups = MappingProxyType(dict(overlap_groups))

        self._transitions = tuple(MappingProxyType(node) for node in transitions)
        self._terminals = tuple(terminals)

    def match_text(self, text: str) -> tuple[ScalableLiteralMatch, ...]:
        normalized_text = normalize_literal_text(text)
        if not normalized_text or len(self._transitions) == 1:
            return ()

        candidates: list[ScalableLiteralMatch] = []
        for start in range(len(normalized_text)):
            node_index = 0
            for end in range(start, len(normalized_text)):
                next_index = self._transitions[node_index].get(normalized_text[end])
                if next_index is None:
                    break
                node_index = next_index
                terminal = self._terminals[node_index]
                if terminal is not None:
                    key, pattern = terminal
                    candidates.append(
                        ScalableLiteralMatch(
                            key=key,
                            pattern=pattern,
                            start=start,
                            end=end + 1,
                        )
                    )

        accepted: list[ScalableLiteralMatch] = []
        occupied_by_group: dict[str, bytearray] = {}
        for candidate in sorted(
            candidates,
            key=lambda item: (-(item.end - item.start), item.start, item.key),
        ):
            group = self._overlap_groups.get(candidate.key, "__default__")
            occupied = occupied_by_group.setdefault(
                group,
                bytearray(len(normalized_text)),
            )
            if any(occupied[candidate.start : candidate.end]):
                continue
            accepted.append(candidate)
            occupied[candidate.start : candidate.end] = b"\x01" * (
                candidate.end - candidate.start
            )

        accepted.sort(
            key=lambda item: (item.start, -(item.end - item.start), item.key)
        )
        unique: list[ScalableLiteralMatch] = []
        seen_match_keys: set[str] = set()
        for match in accepted:
            if match.key in seen_match_keys:
                continue
            seen_match_keys.add(match.key)
            unique.append(match)
        return tuple(unique)


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
