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
class PersonBiography:
    offices: str
    activities: str
    identity_confirmed: bool = True
    different_candidate_name: str = ""

    def describe(self) -> str:
        label = "" if self.identity_confirmed else "同名公开人物，尚不能确认词库所指；"
        if not self.identity_confirmed and self.different_candidate_name:
            label = f"公开人物候选：{self.different_candidate_name}（姓名与词库不同），尚不能确认词库所指；"
        return label + f"任职：{self.offices}" + (f"；公开履历：{self.activities}" if self.activities else "")


@dataclass(frozen=True)
class PersonProfile:
    name: str
    people: tuple[PersonBiography, ...]

    def describe(self) -> str:
        if len(self.people) == 1:
            return self.people[0].describe()
        return "；".join(
            f"同名记录{index}：{person.describe()}"
            for index, person in enumerate(self.people, 1)
        )


class PersonProfileIndex:
    """A startup snapshot. Missing/invalid data must not disable keyword alerts.

    Entity references AND canonical names must agree. A catalog cluster may
    represent several people with the same name: retain all reviewed identities
    and present them separately. This does not identify the message's referent.
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
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("profile file too large")
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("version") != 2 or not isinstance(document.get("profiles"), list):
            raise ValueError("invalid profile document")
        if document.get("content_scope") != "offices_and_public_activities_only":
            raise ValueError("invalid profile content scope")
        entries = document["profiles"]
        if len(entries) > 10_000:
            raise ValueError("too many profiles")
        profiles = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("invalid profile entry")
            ref = _text(entry.get("entity_ref"), 128)
            name = _text(entry.get("name"), 64)
            if ref in profiles:
                raise ValueError("duplicate profile")
            people = entry.get("people")
            if not isinstance(people, list) or not 1 <= len(people) <= 16:
                raise ValueError("invalid profile identities")
            identities: dict[str, bool] = {}
            biographies = []
            for person in people:
                if not isinstance(person, dict) or person.get("sensitive_events_excluded") is not True:
                    raise ValueError("profile needs content review")
                if person.get("review_status") not in {"verified", "source_checked"}:
                    raise ValueError("unreviewed profile")
                if person.get("identity_basis") not in {"curated", "birthdate", "source_record", "public_name_candidate"}:
                    raise ValueError("profile identity not checked")
                confirmed = person["identity_basis"] != "public_name_candidate"
                candidate_name = person.get("candidate_name", name)
                candidate_name = _text(candidate_name, 64)
                if confirmed and candidate_name != name:
                    raise ValueError("confirmed profile name mismatch")
                date.fromisoformat(person["reviewed_on"])
                source_ids = person.get("source_identity_refs")
                if not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 16:
                    raise ValueError("profile needs identity provenance")
                person_ids = set()
                for source_id in source_ids:
                    source_id = _text(source_id, 128)
                    if source_id in person_ids or (source_id in identities and (confirmed or identities[source_id])):
                        raise ValueError("duplicate profile identity")
                    person_ids.add(source_id)
                    identities[source_id] = confirmed
                offices = _text(person.get("offices"), 100)
                activities = person.get("activities", "")
                if activities != "":
                    activities = _text(activities, 100)
                sources = person.get("sources")
                if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
                    raise ValueError("profile needs sources")
                for source in sources:
                    url = urlsplit(_text(source, 500))
                    if url.scheme != "https" or not url.hostname or url.username or url.password:
                        raise ValueError("invalid profile source")
                biographies.append(PersonBiography(offices, activities, confirmed,
                                                   candidate_name if candidate_name != name else ""))
            if type(entry.get("identity_count")) is not int or entry["identity_count"] != len(identities):
                raise ValueError("profile identity coverage incomplete")
            profile = PersonProfile(name, tuple(biographies))
            if len(profile.describe()) > 1000:
                raise ValueError("profile text exceeds display budget")
            profiles[ref] = profile
        return cls(profiles)


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("invalid profile text")
    if value != value.strip() or any((unicodedata.category(c).startswith("C") or unicodedata.category(c) in {"Zl", "Zp"}) for c in value):
        raise ValueError("invalid profile text")
    if "[" in value or "]" in value:
        raise ValueError("profile text must not contain message codes")
    return value
