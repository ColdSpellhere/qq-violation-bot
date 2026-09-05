from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from plugins.content_alert.engine import (
    LiteralKeywordMatcher, ScalableLiteralScanLimitError, match_message_text_segments,
)
from plugins.content_alert.rules import KeywordRule, KeywordRuleStore, normalize_literal_text
from plugins.content_alert.service import _compiled_literal_rules, _match_rules


def reference_matches(rules, text):
    """Frozen pre-change longest-first contract, independent of the new bitmap."""
    text = normalize_literal_text(text)
    occurrences = []
    for rule in rules:
        pattern = normalize_literal_text(rule.pattern)
        start = text.find(pattern)
        while start >= 0:
            occurrences.append((rule.rule_id, start, start + len(pattern)))
            start = text.find(pattern, start + 1)
    accepted = []
    for candidate in sorted(occurrences, key=lambda item: (-(item[2] - item[1]), item[1], item[0])):
        if not any(candidate[1] < old[2] and old[1] < candidate[2] for old in accepted):
            accepted.append(candidate)
    seen = set()
    result = []
    for item in sorted(accepted, key=lambda item: (item[1], -(item[2] - item[1]), item[0])):
        if item[0] not in seen:
            seen.add(item[0])
            result.append(item)
    return result


class BoundedLiteralScanTests(unittest.TestCase):
    def test_bitmap_preserves_previous_longest_first_contract(self):
        rng = random.Random(8137)
        patterns = ('aa', 'aaa', 'ab', 'ba', 'aab', 'baba', 'bba', 'bb')
        for _ in range(200):
            selected = rng.sample(patterns, rng.randint(1, len(patterns)))
            rules = tuple(KeywordRule(f'K{n:04d}', pattern) for n, pattern in enumerate(selected, 1))
            text = ''.join(rng.choice('aabbb Ａ\u200b') for _ in range(50))
            actual = [(m.rule_id, m.start, m.end) for m in LiteralKeywordMatcher(rules).match_text(text)]
            self.assertEqual(reference_matches(rules, text), actual)

    def test_raw_normalized_candidate_and_segment_budgets_fail_closed(self):
        matcher = LiteralKeywordMatcher((KeywordRule('K0001', '合成'),))
        with self.assertRaises(ScalableLiteralScanLimitError):
            matcher.match_text('合成' + 'a' * 16384)
        with self.assertRaises(ScalableLiteralScanLimitError):
            matcher.match_text('\ufb03' * 6000)
        many = LiteralKeywordMatcher(tuple(KeywordRule(f'K{n:04d}', 'a' * (n + 1)) for n in range(1, 10)))
        with self.assertRaises(ScalableLiteralScanLimitError):
            many.match_text('a' * 3000)
        with self.assertRaises(ScalableLiteralScanLimitError):
            match_message_text_segments(Message([MessageSegment.text('x')] * 257), matcher)
        with self.assertRaises(ScalableLiteralScanLimitError):
            match_message_text_segments(Message([MessageSegment.text('a' * 9000)] * 2), matcher)

    def test_rule_snapshot_cache_reuses_compilation_and_invalidates_on_change(self):
        _compiled_literal_rules.cache_clear()
        with tempfile.TemporaryDirectory() as temporary:
            store = KeywordRuleStore(Path(temporary).resolve() / 'keywords.json')
            store.add('合成甲词', actor='synthetic')
            with patch('plugins.content_alert.service.LiteralKeywordMatcher', wraps=LiteralKeywordMatcher) as compiler:
                for _ in range(4):
                    _match_rules(Message('合成甲词'), store.snapshot())
                self.assertEqual(1, compiler.call_count)
                store.add('合成乙词', actor='synthetic')
                result = _match_rules(Message('合成乙词'), store.snapshot())
                self.assertEqual('合成乙词', result[0].pattern)
                self.assertEqual(2, compiler.call_count)


if __name__ == '__main__':
    unittest.main()
