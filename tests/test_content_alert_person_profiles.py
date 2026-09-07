from __future__ import annotations

import json
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests import test_hive_keyword_alert as alert_helpers

from plugins.content_alert.person_profiles import PersonProfileIndex
from plugins.content_alert.service import ContentAlertService, _render_matches
from plugins.content_alert.rules import KeywordRuleStore
from tests.test_hive_keyword_alert import (
    SOURCE_GROUP_ID, REPORT_GROUP_ID, _group_event,
)


def document():
    return {"version": 2, "content_scope": "offices_and_public_activities_only", "profiles": [{
        "entity_ref": "leader.test", "name": "示例人物", "identity_count": 1,
        "people": [{"offices": "曾任示例部门负责人。", "activities": "参与示例公开工程建设。",
                    "source_identity_refs": ["source:1"], "identity_basis": "curated",
                    "review_status": "verified", "reviewed_on": "2026-09-07",
                    "sensitive_events_excluded": True, "sources": ["https://example.invalid/biography"]}],
    }]}


def match(**kwargs):
    fields = dict(term="示例人物", entity_ref="leader.test", entity_refs=(),
                  category_ids=("political_cn",), category_names=("示例分类",),
                  disclosure_policy="management_visible", subject_type="leader_name",
                  match_mode="direct", context_term="", context_class="")
    fields.update(kwargs)
    return SimpleNamespace(**fields)


class PersonProfileTests(unittest.TestCase):
    def load(self, doc):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw).resolve() / "person_profiles.json"
            path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            return PersonProfileIndex.load(path)

    def test_identity_and_name_must_both_agree(self):
        profiles = self.load(document())
        self.assertIsNotNone(profiles.lookup(match()))
        self.assertIsNone(profiles.lookup(match(term="另一个人")))
        self.assertIsNone(profiles.lookup(match(entity_ref="leader.other")))
        self.assertIsNone(profiles.lookup(match(entity_refs=("leader.test", "leader.other"))))
        self.assertIsNone(profiles.lookup(match(entity_ref="")))

    def test_unreviewed_ambiguous_unsafe_and_unattributed_data_rejected(self):
        for field, value in (
            ("review_status", "candidate"), ("identity_count", 2),
            ("sensitive_events_excluded", False), ("sources", []),
            ("sources", ["http://example.invalid/bio"]),
            ("offices", "x" * 101), ("activities", "[CQ:at,qq=all]"),
            ("activities", "换行\n注入"), ("activities", "行\u2028分隔"), ("activities", "段\u2029分隔"), ("reviewed_on", "not-a-date"),
        ):
            with self.subTest(field=field, value=value):
                doc = document()
                target = doc["profiles"][0] if field == "identity_count" else doc["profiles"][0]["people"][0]
                target[field] = value
                with self.assertRaises(ValueError):
                    self.load(doc)
        for doc in ([], {}, {**document(), "profiles": [None]}):
            with self.assertRaises(ValueError):
                self.load(doc)

    def test_person_only_hidden_redaction_and_repeated_hits(self):
        profiles = self.load(document())
        def render(items, hidden=False):
            return _render_matches((), managed_matches=items, strict_hidden=hidden,
                                   political_alert=True, person_profiles=profiles)
        result = render((match(), match()))
        self.assertEqual(result.count("曾任示例部门负责人"), 1)
        self.assertIn("仅姓名匹配", result)
        self.assertNotIn("公开工程", render((match(),), True))
        self.assertNotIn("示例人物", render((match(),), True))
        self.assertNotIn("人物简介", render((match(subject_type="historical_event"),)))
        self.assertNotIn("人物简介", render((match(category_ids=("ordinary",)),)))
        self.assertIn("待补充", render((match(entity_ref="unknown"),)))
        self.assertNotIn("https://", result)

    def test_budget_omits_whole_profile_without_truncating_facts(self):
        profiles = self.load(document())
        result = _render_matches((), managed_matches=(match(),), strict_hidden=False,
                                person_profiles=profiles, max_chars=24)
        self.assertLessEqual(len(result), 24)
        self.assertIn("另有 1 项", result)
        self.assertNotIn("曾任", result)

    def test_same_name_identities_are_all_presented_separately(self):
        doc = document()
        entry = doc["profiles"][0]
        other = copy.deepcopy(entry["people"][0])
        other.update(offices="曾任另一个机构负责人。", activities="",
                     source_identity_refs=["source:2"], identity_basis="birthdate")
        entry["people"].append(other)
        entry["identity_count"] = 2
        profiles = self.load(doc)
        result = _render_matches((), managed_matches=(match(),), strict_hidden=False,
                                 person_profiles=profiles)
        self.assertIn("同名记录1", result)
        self.assertIn("同名记录2", result)
        self.assertIn("另一个机构", result)
        self.assertEqual(result.count("公开履历"), 1)
        entry["identity_count"] = 3
        with self.assertRaisesRegex(ValueError, "coverage incomplete"):
            self.load(doc)
        entry["identity_count"] = 2
        other["source_identity_refs"] = ["source:1"]
        with self.assertRaisesRegex(ValueError, "duplicate profile identity"):
            self.load(doc)

    def test_same_name_budget_requires_complete_bounded_biographies(self):
        doc = document()
        entry = doc["profiles"][0]
        entry["people"] = []
        for index in range(7):
            person = copy.deepcopy(document()["profiles"][0]["people"][0])
            person.update(source_identity_refs=[f"source:{index}"], offices="职" * 100,
                          activities="")
            entry["people"].append(person)
        entry["identity_count"] = 7
        profile = self.load(doc).lookup(match())
        self.assertEqual(len(profile.people), 7)
        self.assertLessEqual(len(profile.describe()), 1000)
        for person in entry["people"]:
            person["activities"] = "事" * 100
        with self.assertRaisesRegex(ValueError, "display budget"):
            self.load(doc)

    def test_uncertain_candidates_are_explicit_and_never_claim_identity(self):
        doc = document()
        first = doc['profiles'][0]['people'][0]
        first['identity_basis'] = 'public_name_candidate'
        second = copy.deepcopy(first)
        second.update(offices='曾任另一公开机构负责人。', activities='')
        doc['profiles'][0]['people'].append(second)
        profiles = self.load(doc)
        text = _render_matches((), managed_matches=(match(),), strict_hidden=False,
                               person_profiles=profiles)
        self.assertEqual(text.count('同名公开人物，尚不能确认词库所指'), 2)
        self.assertIn('另一公开机构', text)
        hidden = _render_matches((), managed_matches=(match(),), strict_hidden=True,
                                 person_profiles=profiles)
        self.assertNotIn('公开机构', hidden)
        self.assertNotIn('示例人物', hidden)
        second['identity_basis'] = 'curated'
        with self.assertRaisesRegex(ValueError, 'duplicate profile identity'):
            self.load(doc)
        doc['profiles'][0]['people'] = [first]
        first['source_identity_refs'] = ['source:1', 'source:1']
        with self.assertRaisesRegex(ValueError, 'duplicate profile identity'):
            self.load(doc)

    def test_different_public_spelling_is_visible_and_never_confirmed(self):
        doc=document()
        person=doc['profiles'][0]['people'][0]
        person.update(identity_basis='public_name_candidate', candidate_name='另一公开姓名')
        text=self.load(doc).lookup(match()).describe()
        self.assertIn('另一公开姓名（姓名与词库不同）', text)
        self.assertIn('尚不能确认词库所指', text)
        self.assertNotIn('同名公开人物', text)
        person['identity_basis']='curated'
        with self.assertRaisesRegex(ValueError, 'name mismatch'):
            self.load(doc)


class PersonProfileDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_is_part_of_delivered_report_and_startup_snapshot(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw).resolve() / "person_profiles.json"
            path.write_text(json.dumps(document(), ensure_ascii=False), encoding="utf-8")
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(raw).resolve() / "keywords.json"),
                managed_catalog=alert_helpers.ContentAlertServiceTests.ManagedCatalog((match(),)),
                source_group_labels={SOURCE_GROUP_ID: "测试群"}, report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(), runtime_enabled=lambda: True, clock=lambda: 2000,
            )
            path.write_text("invalid", encoding="utf-8")
            bot = alert_helpers.ContentAlertServiceTests.Bot()
            self.assertTrue(await service.handle_event(bot, _group_event("示例人物")))
            sent = str(bot.calls[0])
            self.assertIn("参与示例公开工程建设", sent)
            self.assertEqual(len(bot.calls), 1)
            self.assertFalse(await service.handle_event(bot, _group_event("示例人物")))
            with sqlite3.connect(service.outbox.path) as connection:
                report, status = connection.execute(
                    "SELECT report_text,status FROM content_alert_outbox"
                ).fetchone()
            self.assertIn("参与示例公开工程建设", report)
            self.assertNotIn("https://", report)
            self.assertLessEqual(len(report), 1800)
            self.assertEqual(status, "delivered")

    async def test_invalid_profile_file_does_not_suppress_alert(self):
        with tempfile.TemporaryDirectory() as raw:
            (Path(raw).resolve() / "person_profiles.json").write_text("[]", encoding="utf-8")
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(raw).resolve() / "keywords.json"),
                managed_catalog=alert_helpers.ContentAlertServiceTests.ManagedCatalog((match(),)),
                source_group_labels={SOURCE_GROUP_ID: "测试群"}, report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(), runtime_enabled=lambda: True, clock=lambda: 2000,
            )
            bot = alert_helpers.ContentAlertServiceTests.Bot()
            self.assertTrue(await service.handle_event(bot, _group_event("示例人物")))
            self.assertIn("待补充", str(bot.calls[0]))
