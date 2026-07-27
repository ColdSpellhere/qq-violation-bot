from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from plugins.violation_record import db, service
from plugins.violation_record.config import CONFIG


class QueryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        database_path = Path(self.temp.name) / "business.db"
        self.config_patch = patch.object(
            db,
            "CONFIG",
            replace(
                CONFIG,
                database_path=database_path,
                database_url=f"sqlite:///{database_path}",
            ),
        )
        self.config_patch.start()
        db.init_db()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO admins(qq_number,nickname,aliases,is_active,created_at,updated_at) VALUES('90001','记录员','[]',1,?,?)",
                (db.now_str(), db.now_str()),
            )
            conn.execute(
                "INSERT INTO members(qq_number,qq_nickname,aliases,created_at,updated_at) VALUES('123456','小明','[]',?,?)",
                (db.now_str(), db.now_str()),
            )
            member_id = conn.execute(
                "SELECT id FROM members WHERE qq_number='123456'"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO member_group_states(member_id,group_area,status,locked,total_count,deduct_count,current_count_cache,created_at,updated_at) VALUES(?, '蜂巢', '正常', 0, 0, 0, 0, ?, ?)",
                (member_id, db.now_str(), db.now_str()),
            )
            for when, judgement, action in (
                ("2026-07-02 10:00:00", "刷屏", "禁言10分钟"),
                ("2026-07-01 09:00:00", "引战", "警告"),
            ):
                conn.execute(
                    "INSERT INTO violation_records(member_id,group_area,violation_time,judgement,action,remark,is_countable,count_delta,is_test,created_at,updated_at) VALUES(?, '蜂巢', ?, ?, ?, '无', 1, 1, 0, ?, ?)",
                    (member_id, when, judgement, action, db.now_str(), db.now_str()),
                )

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.temp.cleanup()

    def test_member_query_text_and_order_are_locked(self) -> None:
        intent = {
            "group_area": "蜂巢",
            "target": {"qq_number": "123456", "qq_nickname": None},
            "query": {"recent_days": 14},
        }
        result = service.query_member(intent, "90001", "记录员", False, "m1")
        self.assertEqual(
            "小明（123456）\n\n当前次数：2\n状态：正常\n\n具体记录：\n\n"
            "1. 2026/7/2 10:00，刷屏，禁言10分钟\n"
            "2. 2026/7/1 09:00，引战，警告",
            result,
        )

    def test_area_query_text_and_order_are_locked(self) -> None:
        intent = {
            "group_area": "蜂巢",
            "query": {"time_range": "all", "limit": 20},
            "_raw": "蜂巢违规记录",
        }
        result = service.query_area_records(intent, "90001", "记录员", "m2")
        self.assertEqual(
            "蜂巢全部违规记录\n\n记录数：2\n\n具体记录：\n\n"
            "1. 小明（123456） 2026/7/2 10:00，刷屏，禁言10分钟\n"
            "2. 小明（123456） 2026/7/1 09:00，引战，警告",
            result,
        )


if __name__ == "__main__":
    unittest.main()
