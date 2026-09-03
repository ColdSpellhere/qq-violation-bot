from __future__ import annotations

from array import array
from collections import Counter
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


class ScalableLiteralScanLimitError(RuntimeError):
    """Raised without content details when an untrusted scan exceeds a budget."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("managed literal scan limit exceeded")


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
        max_text_chars: int | None = None,
        max_candidates: int | None = None,
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
        for label, limit in (
            ("max_text_chars", max_text_chars),
            ("max_candidates", max_candidates),
        ):
            if limit is not None and (
                isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
            ):
                raise ValueError(f"{label} must be a positive integer")

        # Each node maps one normalized character to the next node index.
        # Terminals contain exactly one key because normalized patterns are
        # required to be unique before the trie is published.
        transitions: list[dict[str, int]] = [{}]
        terminals: list[int | None] = [None]
        records: list[tuple[str, str]] = []
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
            records.append((key, pattern))
            terminals[node_index] = len(records) - 1

        if overlap_groups is None:
            self._overlap_groups = MappingProxyType({})
        else:
            if set(overlap_groups) != seen_keys or any(
                not isinstance(group, str) or not group
                for group in overlap_groups.values()
            ):
                raise ValueError("managed literal overlap groups are invalid")
            self._overlap_groups = MappingProxyType(dict(overlap_groups))

        groups = tuple(
            self._overlap_groups.get(key, "__default__") for key, _pattern in records
        )
        self._transitions = tuple(MappingProxyType(node) for node in transitions)
        self._terminals = tuple(terminals)
        self._records = tuple(records)
        self._groups = groups
        self._group_sizes = MappingProxyType(dict(Counter(groups)))
        self._max_text_chars = max_text_chars
        self._max_candidates = max_candidates

    def match_text(self, text: str) -> tuple[ScalableLiteralMatch, ...]:
        matches = self.match_text_all(text)
        unique: list[ScalableLiteralMatch] = []
        seen_match_keys: set[str] = set()
        for match in matches:
            if match.key in seen_match_keys:
                continue
            seen_match_keys.add(match.key)
            unique.append(match)
        return tuple(unique)

    def match_text_all(self, text: str) -> tuple[ScalableLiteralMatch, ...]:
        """Return every accepted occurrence, including repeated keys.

        Catalog-level compound matching must inspect each occurrence: an early
        leader-name mention can lack context while a later mention of the same
        name has qualifying context.  ``match_text`` retains the historical
        one-result-per-key contract for existing callers.
        """

        if len(self._transitions) == 1:
            return ()
        if self._max_text_chars is not None and len(text) > self._max_text_chars:
            raise ScalableLiteralScanLimitError("raw_text_limit")
        normalized_text = normalize_literal_text(text)
        if not normalized_text:
            return ()
        if (
            self._max_text_chars is not None
            and len(normalized_text) > self._max_text_chars
        ):
            raise ScalableLiteralScanLimitError("normalized_text_limit")

        accepted: list[ScalableLiteralMatch] = []
        # Only candidates from groups containing competing keys need overlap
        # resolution.  Compact integer buckets preserve the historical
        # longest-first ordering without allocating one Python object per
        # rejected nested occurrence.
        candidates_by_length: dict[int, array[int]] = {}
        record_count = len(self._records)
        candidate_count = 0
        for start in range(len(normalized_text)):
            node_index = 0
            for end in range(start, len(normalized_text)):
                next_index = self._transitions[node_index].get(normalized_text[end])
                if next_index is None:
                    break
                node_index = next_index
                terminal = self._terminals[node_index]
                if terminal is not None:
                    candidate_count += 1
                    if (
                        self._max_candidates is not None
                        and candidate_count > self._max_candidates
                    ):
                        raise ScalableLiteralScanLimitError("candidate_limit")
                    key, pattern = self._records[terminal]
                    group = self._groups[terminal]
                    match_length = end + 1 - start
                    if self._group_sizes[group] == 1:
                        accepted.append(
                            ScalableLiteralMatch(
                                key=key,
                                pattern=pattern,
                                start=start,
                                end=end + 1,
                            )
                        )
                        continue
                    bucket = candidates_by_length.setdefault(
                        match_length,
                        array("Q"),
                    )
                    bucket.append(start * record_count + terminal)

        # Accepted intervals of different keys in a group are disjoint.  A
        # sparse owner map grows only with actual covered positions; it avoids
        # allocating len(text) cells for every singleton support-context group.
        owners_by_group: dict[str, dict[int, int]] = {}
        for match_length in sorted(candidates_by_length, reverse=True):
            for encoded in candidates_by_length[match_length]:
                start, record_index = divmod(encoded, record_count)
                key, pattern = self._records[record_index]
                group = self._groups[record_index]
                owners = owners_by_group.setdefault(group, {})
                end = start + match_length
                if any(
                    owner is not None and owner != record_index
                    for offset in range(start, end)
                    if (owner := owners.get(offset)) is not None
                ):
                    continue
                accepted.append(
                    ScalableLiteralMatch(
                        key=key,
                        pattern=pattern,
                        start=start,
                        end=end,
                    )
                )
                for offset in range(start, end):
                    owners[offset] = record_index

        accepted.sort(key=lambda item: (item.start, -(item.end - item.start), item.key))
        return tuple(accepted)


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
