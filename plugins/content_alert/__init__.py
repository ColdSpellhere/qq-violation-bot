"""Literal, instance-local content alert primitives.

The package deliberately contains no model integration.  The matcher consumes
only explicit text segments and the rule store persists rule metadata only.
"""

from .engine import KeywordMatch, LiteralKeywordMatcher, match_message_text_segments
from .rules import KeywordRule, KeywordRuleStore

__all__ = (
    "KeywordMatch",
    "KeywordRule",
    "KeywordRuleStore",
    "LiteralKeywordMatcher",
    "match_message_text_segments",
)
