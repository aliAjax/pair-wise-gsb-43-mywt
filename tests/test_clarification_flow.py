import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402


class ClarificationRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "CV-001", "启明科技", "vendor1")
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "CV-002", "远山系统", "vendor2")
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
        tender = self.service.create_tender("proc1", "procurement", "CT-001", "网络设备", deadline, criteria)
        self.tender = self.service.publish_tender("proc1", "procurement", tender["id"], tender["version"])
        self.bid1 = self.service.submit_bid(
            "vendor1", "vendor", self.tender["id"], self.vendor1["id"],
            {"报价": 800000, "质量": 90}, 800000
        )
        self.bid2 = self.service.submit_bid(
            "vendor2", "vendor", self.tender["id"], self.vendor2["id"],
            {"报价": 700000, "质量": 80}, 700000
        )

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, number="CL-001", answer="功率参数调整", affected=None, resubmit=True):
        return self.service.publish_clarification(
            "proc1", "procurement", self.tender["id"], number, answer,
            affected_vendor_ids=affected, requires_resubmission=resubmit,
            expected_version=self.tender["version"],
        )

    def test_clarification_invalidates_stale_bids_until_confirm_or_resubmit(self):
        result = self.publish(affected=[self.vendor1["id"]])
        self.assertEqual(2, result["tender"]["clarification_version"])
        self.assertEqual([self.bid1["id"], self.bid2["id"]], result["affected_bid_ids"])

        with self.assertRaises(DomainError) as ctx:
            self.service.open_bids("proc1", "procurement", self.tender["id"], result["tender"]["version"] + 1)
        self.assertEqual(409, ctx.exception.status)

        vendor1_view = self.service.get_bid("vendor1", "vendor", self.bid1["id"])
        self.assertEqual("pending", vendor1_view["clarification_status"][0]["status"])
        self.assertTrue(vendor1_view["clarification_status"][0]["requires_resubmission"])
        with self.assertRaises(DomainError):
            self.service.confirm_bid("vendor1", "vendor", self.bid1["id"], self.bid1["version"])
        resubmitted = self.service.submit_bid(
            "vendor1", "vendor", self.tender["id"], self.vendor1["id"],
            {"报价": 790000, "质量": 95}, 790000, expected_version=self.bid1["version"],
        )
        self.assertEqual(2, resubmitted["clarification_version"])
        self.assertEqual(self.bid1["submitted_at"], resubmitted["original_submitted_at"])
        self.assertEqual(self.bid1["version"] + 1, resubmitted["version"])

        vendor2_view = self.service.get_bid("vendor2", "vendor", self.bid2["id"])
        confirmed = self.service.confirm_bid(
            "vendor2", "vendor", self.bid2["id"], vendor2_view["version"]
        )
        self.assertEqual(2, confirmed["clarification_version"])
        self.assertEqual(self.bid2["submitted_at"], confirmed["submitted_at"])

        time.sleep(2.1)
        current = self.service.get_tender("proc1", "procurement", self.tender["id"])["tender"]
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], current["version"])
        self.assertEqual("opened", opened["tender"]["status"])
        self.assertEqual(2, len(opened["bids"]))

    def test_published_clarification_is_immutable_and_loser_gets_conflict_without_partial_open(self):
        first = self.publish()
        with self.assertRaises(DomainError) as ctx:
            self.publish(answer="参数被二次修改")
        self.assertEqual(409, ctx.exception.status)

        # Open and clarification both start from the same pre-clarification version.
        # Clarification has committed first; the open request must fail before opening any bid.
        with self.assertRaises(DomainError) as loser:
            self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.assertEqual(409, loser.exception.status)
        stored = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual("published", stored["tender"]["status"])
        self.assertTrue(all(bid["status"] == "sealed" for bid in stored["bids"]))
        self.assertEqual(2, stored["tender"]["clarification_version"])
        self.assertEqual(first["version"]["version"], 2)

    def test_retry_same_clarification_number_completes_missing_parts(self):
        published = self.publish(affected=[self.vendor1["id"]])
        conn = self.service.connect()
        conn.execute("DELETE FROM bid_clarification_status WHERE clarification_version=2")
        conn.execute("DELETE FROM timeline WHERE event_key=?", ("clarification-2-bid-%s" % self.bid1["id"],))
        conn.commit()
        conn.close()

        retried = self.service.publish_clarification(
            "proc1", "procurement", self.tender["id"], "CL-001", "功率参数调整",
            affected_vendor_ids=[self.vendor1["id"]], expected_version=self.tender["version"],
        )
        self.assertTrue(retried["replayed"])
        self.assertTrue(retried["repaired"])
        self.assertEqual([self.bid1["id"], self.bid2["id"]], retried["affected_bid_ids"])
        statuses = conn_execute(self.service, (
            "SELECT clarification_version,status,requires_resubmission FROM bid_clarification_status "
            "WHERE bid_id=? ORDER BY id"
        ), (self.bid1["id"],))
        self.assertEqual([(2, "pending", 1)], statuses)
        events = conn_execute(self.service, (
            "SELECT action FROM timeline WHERE event_key=? OR event_key=?"
        ), ("clarification-published-CL-001", "clarification-2-bid-%s" % self.bid1["id"]))
        self.assertIn("clarification.published", [row[0] for row in events])
        self.assertIn("clarification.bid_invalidated", [row[0] for row in events])
        self.assertEqual(published["version"]["content_hash"], retried["version"]["content_hash"])

    def test_retry_repairs_missing_published_audit_without_creating_new_version(self):
        published = self.publish(number="CL-002", affected=[self.vendor1["id"]])
        conn = self.service.connect()
        conn.execute("DELETE FROM timeline WHERE event_key=?", ("clarification-published-CL-002",))
        conn.commit()
        conn.close()
        retried = self.service.publish_clarification(
            "proc1", "procurement", self.tender["id"], "CL-002", "功率参数调整",
            affected_vendor_ids=[self.vendor1["id"]], expected_version=self.tender["version"],
        )
        self.assertTrue(retried["replayed"])
        self.assertTrue(retried["repaired"])
        self.assertEqual(published["version"]["id"], retried["version"]["id"])
        self.assertEqual(1, conn_execute(
            self.service,
            "SELECT COUNT(*) FROM timeline WHERE event_key=?",
            ("clarification-published-CL-002",),
        )[0][0])

    def test_vendor_cannot_read_other_bid_payload(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.get_bid("vendor2", "vendor", self.bid1["id"])
        self.assertEqual(403, ctx.exception.status)
        own = self.service.get_bid("vendor1", "vendor", self.bid1["id"])
        self.assertNotIn("payload", own)

    def test_legacy_bids_without_clarification_version_are_backfilled_as_initial(self):
        import sqlite3

        db_path = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE tenders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tender_no TEXT UNIQUE, title TEXT,
                description TEXT DEFAULT '', status TEXT, deadline TEXT, criteria TEXT DEFAULT '[]',
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
                payload TEXT, payload_hash TEXT, price REAL, status TEXT DEFAULT 'sealed',
                version INTEGER DEFAULT 1, submitted_by TEXT, submitted_at TEXT, opened_at TEXT
            );
            CREATE TABLE clarifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tender_id INTEGER, vendor_id INTEGER,
                question TEXT, answer TEXT, status TEXT DEFAULT 'pending',
                answered_by TEXT, created_at TEXT, answered_at TEXT
            );
            CREATE TABLE evaluations (id INTEGER PRIMARY KEY, bid_id INTEGER, evaluation_round INTEGER,
                evaluator TEXT, criterion TEXT, raw_value REAL, score REAL, comment TEXT DEFAULT '',
                version INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT);
            CREATE TABLE conflicts (id INTEGER PRIMARY KEY, tender_id INTEGER, evaluator TEXT,
                vendor_id INTEGER, reason TEXT, declared_by TEXT, created_at TEXT);
            CREATE TABLE complaints (id INTEGER PRIMARY KEY, tender_id INTEGER, complainant TEXT,
                body TEXT, status TEXT DEFAULT 'open', resolution TEXT, reviewed_by TEXT,
                created_at TEXT, resolved_at TEXT);
            CREATE TABLE timeline (id INTEGER PRIMARY KEY, tender_id INTEGER, actor TEXT,
                action TEXT, details TEXT, created_at TEXT);
            INSERT INTO tenders(tender_no,title,status,deadline,created_by,created_at,updated_at)
            VALUES('OLD-1','旧项目','published','2099-01-01T00:00:00+00:00','proc','2026-01-01','2026-01-01');
            INSERT INTO vendors(vendor_no,name,representative,created_at)
            VALUES('OLD-V','旧供应商','old-vendor','2026-01-01');
            INSERT INTO bids(tender_id,vendor_id,payload,payload_hash,price,submitted_by,submitted_at)
            VALUES(1,1,'{}','x',100,'old-vendor','2026-01-02T03:04:05+00:00');
            """
        )
        conn.commit()
        conn.close()

        migrated = ProcurementService(Path(self.tmp.name) / "legacy.db")
        bid = migrated.get_bid("proc1", "procurement", 1)
        tender = migrated.get_tender("proc1", "procurement", 1)["tender"]
        self.assertEqual(1, bid["clarification_version"])
        self.assertEqual(1, tender["clarification_version"])
        self.assertEqual("2026-01-02T03:04:05+00:00", bid["original_submitted_at"])
        self.assertEqual("2026-01-02T03:04:05+00:00", bid["submitted_at"])


def conn_execute(service, sql, params=()):
    conn = service.connect()
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main()
