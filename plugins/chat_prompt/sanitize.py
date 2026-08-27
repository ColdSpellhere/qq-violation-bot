from __future__ import annotations

import re


_ROLE_MARKER = re.compile(
    r"(?im)^(?P<indent>\s*)\[(?P<role>assistant|system|developer|user)\]"
)


def neutralize_role_markers(value: str) -> str:
    """Make copied chat role labels visibly inert before prompt rendering."""

    return _ROLE_MARKER.sub(
        lambda match: (
            f"{match.group('indent')}［quoted-{match.group('role').casefold()}］"
        ),
        value,
    )


__all__ = ["neutralize_role_markers"]
