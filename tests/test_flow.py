import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402


class ProcurementFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "V-001", "启明科技", "vendor1")
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "V-002", "远山系统", "vendor2")
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-001", "数据中心设备", (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def bid(self, vendor, actor, number, price, quality):
        return self.service.submit_bid(actor, "vendor", self.tender["id"], vendor["id"], {"报价": price, "质量": quality}, price)

    def test_complete_sealed_bid_open_evaluate_and_award_flow(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        before = self.service.get_tender("vendor1", "vendor", self.tender["id"])
        self.assertEqual("sealed", before["bids"][0]["status"])
        self.assertNotIn("payload", before["bids"][0])
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.assertEqual(2, len(opened["bids"]))
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][1]["id"], {"报价": 700000, "质量": 80})
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("opened", current["tender"]["status"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        self.assertEqual(opened["bids"][0]["id"], award["award"]["winner"]["bid_id"])

    def test_conflict_and_duplicate_evaluation_are_rejected(self):
        bid = self.bid(self.vendor1, "vendor1", "B3", 800000, 90)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.declare_conflict("eval1", "evaluator", self.tender["id"], "eval1", self.vendor1["id"], "曾受雇于供应商")
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(403, ctx.exception.status)
        self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        with self.assertRaises(DomainError) as ctx2:
            self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx2.exception.status)

    def test_complaint_reevaluation_award_block_and_permissions(self):
        bid = self.bid(self.vendor1, "vendor1", "B4", 800000, 90)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        complaint = self.service.submit_complaint("vendor1", "vendor", self.tender["id"], "评分标准理解有误")
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        with self.assertRaises(DomainError):
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        resolved = self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "accepted", "按新规则重评")
        self.assertEqual("accepted", resolved["status"])
        updated = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        self.assertEqual("reevaluation", updated["status"])
        self.assertEqual(2, updated["evaluation_round"])
        with self.assertRaises(DomainError) as ctx:
            self.service.open_bids("vendor1", "vendor", self.tender["id"], updated["version"])
        self.assertEqual(403, ctx.exception.status)
    def test_clarification_version_requires_resubmission_before_opening(self):
        bid1 = self.bid(self.vendor1, "vendor1", "B5", 800000, 90)
        bid2 = self.bid(self.vendor2, "vendor2", "B6", 700000, 80)
        current = self.service.get_tender("proc1", "procurement", self.tender["id"])["tender"]
        published = self.service.publish_clarification(
            "proc1",
            "procurement",
            self.tender["id"],
            "CLAR-001",
            "电源参数是否调整？",
            "必须支持双电源",
            self.vendor1["id"],
            expected_version=current["version"],
            affected_vendor_ids=[self.vendor1["id"], self.vendor2["id"]],
        )
        self.assertEqual(2, published["clarification"]["target_version"])
        self.assertEqual(2, len(published["invalidations"]))
        stale_bid = self.service.get_bid("vendor1", "vendor", bid1["id"])
        self.assertEqual("pending_confirmation", stale_bid["status"])
        self.assertEqual("pending", stale_bid["confirmation_status"])
        with self.assertRaises(DomainError) as blocked:
            self.service.open_bids("proc1", "procurement", self.tender["id"], current["version"] + 1)
        self.assertEqual(409, blocked.exception.status)
        with self.assertRaises(DomainError) as must_resubmit:
            self.service.confirm_bid("vendor1", "vendor", bid1["id"], stale_bid["version"])
        self.assertEqual(409, must_resubmit.exception.status)
        with self.assertRaises(DomainError) as denied:
            self.service.get_bid("vendor2", "vendor", bid1["id"])
        self.assertEqual(403, denied.exception.status)
        refreshed1 = self.service.get_bid("vendor1", "vendor", bid1["id"])
        refreshed2 = self.service.get_bid("vendor2", "vendor", bid2["id"])
        resubmitted1 = self.service.confirm_bid(
            "vendor1", "vendor", bid1["id"], refreshed1["version"], True,
            {"报价": 810000, "质量": 91}, 810000,
        )
        resubmitted2 = self.service.confirm_bid(
            "vendor2", "vendor", bid2["id"], refreshed2["version"], True,
            {"报价": 710000, "质量": 81}, 710000,
        )
        self.assertEqual(2, resubmitted1["clarification_version"])
        self.assertEqual(bid1["submitted_at"], resubmitted1["original_submitted_at"])
        time.sleep(2.1)
        opened = self.service.open_bids(
            "proc1", "procurement", self.tender["id"], current["version"] + 1
        )
        self.assertEqual(2, len(opened["bids"]))
        self.assertEqual(resubmitted2["id"], opened["bids"][1]["id"])

    def test_unaffected_bid_can_confirm_and_immutable_clarification_cannot_change(self):
        bid = self.bid(self.vendor1, "vendor1", "B7", 800000, 90)
        current = self.service.get_tender("proc1", "procurement", self.tender["id"])["tender"]
        published = self.service.publish_clarification(
            "proc1", "procurement", self.tender["id"], "CLAR-002",
            "交付要求？", "保持原交付要求", self.vendor2["id"],
            expected_version=current["version"], affected_vendor_ids=[],
        )
        self.assertEqual(1, len(published["invalidations"]))
        self.assertEqual(0, published["invalidations"][0]["affected"])
        stale = self.service.get_bid("vendor1", "vendor", bid["id"])
        self.assertEqual("pending_confirmation", stale["status"])
        confirmed = self.service.confirm_bid("vendor1", "vendor", bid["id"], stale["version"])
        self.assertEqual("sealed", confirmed["status"])
        self.assertEqual(2, confirmed["clarification_version"])
        with self.assertRaises(DomainError) as changed:
            self.service.publish_clarification(
                "proc1", "procurement", self.tender["id"], "CLAR-002",
                "交付要求？", "必须提前交付", self.vendor2["id"]
            )
        self.assertEqual(409, changed.exception.status)
        version = self.service.get_tender("proc1", "procurement", self.tender["id"])["clarifications"][0]
        with sqlite3.connect(self.service.db_path) as raw:
            raw.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                raw.execute("UPDATE clarification_versions SET answer=? WHERE id=?", ("改答复", version["id"]))

    def test_retry_same_clarification_number_completes_only_missing_parts(self):
        self.bid(self.vendor1, "vendor1", "B8", 800000, 90)
        current = self.service.get_tender("proc1", "procurement", self.tender["id"])["tender"]
        prepared = self.service._prepare_clarification_publication(
            "proc1", self.tender["id"], "CLAR-003", "参数？", "新参数",
            self.vendor1["id"], None, current["version"], [self.vendor1["id"]]
        )
        self.assertEqual("pending", prepared["publication"]["status"])
        pending_view = self.service.get_tender("vendor1", "vendor", self.tender["id"])
        self.assertEqual([], pending_view["clarifications"])
        self.assertNotIn("新参数", str(pending_view))
        with self.assertRaises(DomainError) as conflict:
            self.service.open_bids("proc1", "procurement", self.tender["id"], current["version"])
        self.assertEqual(409, conflict.exception.status)
        completed = self.service._complete_clarification_publication(
            self.tender["id"], prepared["publication"]["id"]
        )
        self.assertEqual("completed", completed["publication"]["status"])
        retried = self.service.publish_clarification(
            "proc1", "procurement", self.tender["id"], "CLAR-003",
            "参数？", "新参数", self.vendor1["id"],
            clarification_id=prepared["publication"]["clarification_id"],
            expected_version=current["version"] + 1,
        )
        self.assertTrue(retried["recovered"])
        self.assertEqual(completed["clarification"]["id"], retried["clarification"]["id"])
        audits = self.service.state("proc1", "procurement")["timeline"]
        self.assertEqual(
            1,
            sum(1 for item in audits
                if item["action"] == "clarification.published" and "CLAR-003" in item["details"]),
        )

    def test_legacy_bids_are_backfilled_to_initial_clarification_version(self):
        db_path = Path(self.tmp.name) / "legacy.db"
        old = sqlite3.connect(db_path)
        old.executescript(
            """
            CREATE TABLE tenders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tender_no TEXT UNIQUE, title TEXT,
                description TEXT DEFAULT '', status TEXT, deadline TEXT, criteria TEXT,
                evaluation_round INTEGER DEFAULT 1, evaluations_locked INTEGER DEFAULT 0,
                awarded_bid_id INTEGER, award_snapshot TEXT, version INTEGER DEFAULT 1,
                created_by TEXT, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE vendors (
                id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_no TEXT UNIQUE, name TEXT,
                representative TEXT, created_at TEXT
            );
            CREATE TABLE bids (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tender_id INTEGER, vendor_id INTEGER,
                payload TEXT, payload_hash TEXT, price REAL, status TEXT, version INTEGER DEFAULT 1,
                submitted_by TEXT, submitted_at TEXT, opened_at TEXT, UNIQUE(tender_id,vendor_id)
            );
            CREATE TABLE evaluations (id INTEGER PRIMARY KEY, bid_id INTEGER, evaluation_round INTEGER, evaluator TEXT, criterion TEXT, raw_value REAL, score REAL, comment TEXT, version INTEGER, created_at TEXT, updated_at TEXT);
            CREATE TABLE conflicts (id INTEGER PRIMARY KEY, tender_id INTEGER, evaluator TEXT, vendor_id INTEGER, reason TEXT, declared_by TEXT, created_at TEXT);
            CREATE TABLE clarifications (id INTEGER PRIMARY KEY, tender_id INTEGER, vendor_id INTEGER, question TEXT, answer TEXT, status TEXT, answered_by TEXT, created_at TEXT, answered_at TEXT);
            CREATE TABLE complaints (id INTEGER PRIMARY KEY, tender_id INTEGER, complainant TEXT, body TEXT, status TEXT, resolution TEXT, reviewed_by TEXT, created_at TEXT, resolved_at TEXT);
            CREATE TABLE timeline (id INTEGER PRIMARY KEY AUTOINCREMENT, tender_id INTEGER, actor TEXT, action TEXT, details TEXT, created_at TEXT);
            INSERT INTO tenders(tender_no,title,status,deadline,criteria,created_by,created_at,updated_at)
            VALUES ('OLD-1','旧项目','published','2099-01-01T00:00:00+00:00','[]','proc1','2024-01-01T00:00:00+00:00','2024-01-01T00:00:00+00:00');
            INSERT INTO vendors(vendor_no,name,representative,created_at) VALUES ('OLD-V','旧供应商','vendor1','2024-01-01T00:00:00+00:00');
            INSERT INTO bids(tender_id,vendor_id,payload,payload_hash,price,status,submitted_by,submitted_at)
            VALUES (1,1,'{}','x',100,'sealed','vendor1','2024-01-02T00:00:00+00:00');
            """
        )
        old.commit()
        old.close()
        service = ProcurementService(db_path)
        bid = service.get_bid("vendor1", "vendor", 1)
        self.assertEqual(1, bid["clarification_version"])
        self.assertEqual("confirmed", bid["confirmation_status"])
        self.assertEqual("2024-01-02T00:00:00+00:00", bid["original_submitted_at"])
        self.assertEqual(bid["original_submitted_at"], bid["submitted_at"])
        audits = service.state("proc1", "procurement")["timeline"]
        self.assertTrue(any(item["action"] == "clarification.version.backfilled" for item in audits))


if __name__ == "__main__":
    unittest.main()
