"""Reviewed, instance-private biographies; never infer identity or call a model."""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class PersonProfile:
    name: str
    offices: str
    activities: str


class PersonProfileIndex:
    """A startup snapshot. Missing/invalid data must not disable keyword alerts.

    Entity references AND canonical names must agree. A catalog cluster may
    represent several people with the same name: only reviewed unambiguous
    profiles belong here. This does not identify the person meant by a message.
    """

    def __init__(self, profiles: Mapping[str, PersonProfile] | None = None):
        self._profiles = MappingProxyType(dict(profiles or {}))

    def lookup(self, match: object) -> PersonProfile | None:
        refs = tuple(getattr(match, "entity_refs", ()))
        ref = getattr(match, "entity_ref", "")
        if not refs and ref:
            refs = (ref,)
        if len(set(refs)) != 1:
            return None
        profile = self._profiles.get(refs[0])
        return profile if profile and profile.name == getattr(match, "term", "") else None

    @classmethod
    def load(cls, path: Path) -> PersonProfileIndex:
        # Bounded read, no network and no file access on the message hot path.
        with path.open("rb") as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("profile file too large")
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("profiles"), list):
            raise ValueError("invalid profile document")
        if document.get("content_scope") != "offices_and_public_activities_only":
            raise ValueError("invalid profile content scope")
        entries = document["profiles"]
        if len(entries) > 10_000:
            raise ValueError("too many profiles")
        profiles = {}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("sensitive_events_excluded") is not True:
                raise ValueError("profile needs content review")
            ref = _text(entry.get("entity_ref"), 128)
            name = _text(entry.get("name"), 64)
            offices = _text(entry.get("offices"), 100)
            activities = _text(entry.get("activities"), 100)
            if ref in profiles or entry.get("review_status") != "verified":
                raise ValueError("unreviewed or duplicate profile")
            date.fromisoformat(entry["reviewed_on"])
            if entry.get("identity_count") != 1:
                raise ValueError("ambiguous profile identity")
            sources = entry.get("sources")
            if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
                raise ValueError("profile needs sources")
            for source in sources:
                url = urlsplit(_text(source, 500))
                if url.scheme != "https" or not url.hostname or url.username or url.password:
                    raise ValueError("invalid profile source")
            profiles[ref] = PersonProfile(name, offices, activities)
        return cls(profiles)


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("invalid profile text")
    if value != value.strip() or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("invalid profile text")
    if "[" in value or "]" in value:
        raise ValueError("profile text must not contain message codes")
    return value
