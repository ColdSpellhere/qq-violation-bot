from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .models import (
    ContextMessageLike,
    MemberProfileLike,
    SpeakerDirectory,
    SpeakerIdentity,
)


def build_speaker_directory(
    *,
    current: ContextMessageLike,
    context: Sequence[ContextMessageLike],
    profiles: Sequence[MemberProfileLike],
) -> SpeakerDirectory:
    identities: list[SpeakerIdentity] = []
    refs_by_user: dict[str, str] = {}
    refs_by_message: dict[str, str] = {}
    identity_index: dict[str, int] = {}
    unknown_count = 0

    def add_known(user_id: object, nickname: object = "", *, current_user: bool = False) -> str | None:
        normalized = str(user_id or "").strip()
        if not normalized:
            return None
        display = str(nickname or "").strip()
        existing = refs_by_user.get(normalized)
        if existing is not None:
            index = identity_index[existing]
            identity = identities[index]
            if (not identity.nickname and display) or current_user:
                identities[index] = replace(
                    identity,
                    nickname=display or identity.nickname,
                    current=identity.current or current_user,
                )
            return existing
        ref = f"S{len(refs_by_user) + 1}"
        refs_by_user[normalized] = ref
        identity_index[ref] = len(identities)
        identities.append(SpeakerIdentity(ref, normalized, display, current_user))
        return ref

    def add_turn(turn: ContextMessageLike, *, current_user: bool = False) -> str:
        nonlocal unknown_count
        ref = add_known(turn.user_id, turn.nickname, current_user=current_user)
        if ref is None:
            unknown_count += 1
            ref = f"U{unknown_count}"
            identity_index[ref] = len(identities)
            identities.append(
                SpeakerIdentity(ref, "", str(turn.nickname or "").strip(), current_user)
            )
        message_id = str(turn.message_id or "").strip()
        if message_id:
            refs_by_message[message_id] = ref
        return ref

    add_turn(current, current_user=True)
    add_known(current.replied_to_user_id)
    for user_id in current.at_user_ids:
        add_known(user_id)
    for turn in context:
        add_turn(turn)
        add_known(turn.replied_to_user_id)
        for user_id in turn.at_user_ids:
            add_known(user_id)
    for profile in profiles:
        add_known(profile.user_id, profile.nickname)

    return SpeakerDirectory(
        identities=tuple(identities),
        refs_by_user=tuple(refs_by_user.items()),
        refs_by_message=tuple(refs_by_message.items()),
    )


__all__ = ["build_speaker_directory"]
