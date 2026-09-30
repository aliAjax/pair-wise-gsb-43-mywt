"""Sealed public-procurement tendering and evaluation service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "public_procurement.db"


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DomainError("时间格式无效") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def clean_actor(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ProcurementService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    deadline TEXT NOT NULL,
                    criteria TEXT NOT NULL DEFAULT '[]',
                    evaluation_round INTEGER NOT NULL DEFAULT 1,
                    evaluations_locked INTEGER NOT NULL DEFAULT 0,
                    awarded_bid_id INTEGER,
                    award_snapshot TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    clarification_version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vendors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vendor_no TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    representative TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'sealed',
                    version INTEGER NOT NULL DEFAULT 1,
                    clarification_version INTEGER NOT NULL DEFAULT 1,
                    original_submitted_at TEXT NOT NULL DEFAULT '',
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    opened_at TEXT,
                    UNIQUE(tender_id,vendor_id)
                );
                CREATE TABLE IF NOT EXISTS evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bid_id INTEGER NOT NULL REFERENCES bids(id),
                    evaluation_round INTEGER NOT NULL,
                    evaluator TEXT NOT NULL,
                    criterion TEXT NOT NULL,
                    raw_value REAL NOT NULL,
                    score REAL NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(bid_id,evaluation_round,evaluator,criterion)
                );
                CREATE TABLE IF NOT EXISTS conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    evaluator TEXT NOT NULL,
                    vendor_id INTEGER REFERENCES vendors(id),
                    reason TEXT NOT NULL,
                    declared_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(tender_id,evaluator,vendor_id)
                );
                CREATE TABLE IF NOT EXISTS clarifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER REFERENCES vendors(id),
                    clarification_no TEXT UNIQUE,
                    question TEXT NOT NULL DEFAULT '',
                    answer TEXT,
                    content_hash TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    release_version INTEGER,
                    answered_by TEXT,
                    created_at TEXT NOT NULL,
                    answered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS clarification_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    clarification_id INTEGER NOT NULL REFERENCES clarifications(id),
                    clarification_no TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL,
                    answer TEXT NOT NULL,
                    affected_vendor_ids TEXT NOT NULL DEFAULT '[]',
                    requires_resubmission INTEGER NOT NULL DEFAULT 1,
                    content_hash TEXT NOT NULL,
                    published_by TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    UNIQUE(tender_id,version)
                );
                CREATE TABLE IF NOT EXISTS bid_clarification_status (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bid_id INTEGER NOT NULL REFERENCES bids(id),
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
                    clarification_version INTEGER NOT NULL,
                    requires_resubmission INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending',
                    resolved_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(bid_id,clarification_version)
                );
                CREATE TABLE IF NOT EXISTS complaints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    complainant TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    resolution TEXT,
                    reviewed_by TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER REFERENCES tenders(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    event_key TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_bids_tender ON bids(tender_id,status);
                CREATE INDEX IF NOT EXISTS idx_bid_clarification_pending
                    ON bid_clarification_status(tender_id,status) WHERE status='pending';
                CREATE INDEX IF NOT EXISTS idx_eval_bid_round ON evaluations(bid_id,evaluation_round);
                """
            )
            self._migrate_schema(conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        def columns(table: str) -> set[str]:
            return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

        tender_columns = columns("tenders")
        if "clarification_version" not in tender_columns:
            conn.execute("ALTER TABLE tenders ADD COLUMN clarification_version INTEGER NOT NULL DEFAULT 1")
        bid_columns = columns("bids")
        if "clarification_version" not in bid_columns:
            conn.execute("ALTER TABLE bids ADD COLUMN clarification_version INTEGER NOT NULL DEFAULT 1")
        if "original_submitted_at" not in bid_columns:
            conn.execute("ALTER TABLE bids ADD COLUMN original_submitted_at TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE bids SET original_submitted_at=submitted_at WHERE original_submitted_at=''")
        clarification_columns = columns("clarifications")
        for statement in (
            "ALTER TABLE clarifications ADD COLUMN clarification_no TEXT",
            "ALTER TABLE clarifications ADD COLUMN content_hash TEXT",
            "ALTER TABLE clarifications ADD COLUMN release_version INTEGER",
        ):
            column = statement.split("ADD COLUMN ")[1].split()[0]
            if column not in clarification_columns:
                conn.execute(statement)
        timeline_columns = columns("timeline")
        if "event_key" not in timeline_columns:
            conn.execute("ALTER TABLE timeline ADD COLUMN event_key TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_timeline_event_key ON timeline(event_key) WHERE event_key IS NOT NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bid_clarification_pending ON bid_clarification_status(tender_id,status) WHERE status='pending'"
        )
        published_rows = conn.execute(
            """SELECT tender_id,id,COALESCE(answered_at,created_at) AS published_at
               FROM clarifications WHERE status='published' ORDER BY tender_id,id"""
        ).fetchall()
        for row in published_rows:
            version = conn.execute(
                """SELECT COALESCE(MAX(version),0)+1 AS next_version
                   FROM clarification_versions WHERE tender_id=?""",
                (row["tender_id"],),
            ).fetchone()["next_version"]
            version = max(version, conn.execute(
                "SELECT COALESCE(MAX(clarification_version),1)+1 FROM tenders WHERE id=?",
                (row["tender_id"],),
            ).fetchone()[0])
            conn.execute(
                """INSERT OR IGNORE INTO clarification_versions(
                       tender_id,clarification_id,clarification_no,version,answer,affected_vendor_ids,
                       requires_resubmission,content_hash,published_by,published_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (row["tender_id"], row["id"],
                 "LEGACY-CL-%s" % row["id"], version,
                 row["answer"] if row["answer"] is not None else "", "[]", 0,
                 "legacy", row["answered_by"] or "legacy", row["published_at"]),
            )
            conn.execute(
                """UPDATE tenders
                   SET clarification_version=MAX(clarification_version,
                       (SELECT version FROM clarification_versions WHERE clarification_id=?)),
                       version=MAX(version,(SELECT version FROM clarification_versions WHERE clarification_id=?))
                   WHERE id=?""",
                (row["id"], row["id"], row["tender_id"]),
            )
            conn.execute(
                """INSERT OR IGNORE INTO bid_clarification_status(
                       bid_id,tender_id,vendor_id,clarification_version,requires_resubmission,status,created_at)
                   SELECT b.id,b.tender_id,b.vendor_id,?,0,'pending',?
                   FROM bids b
                   WHERE b.tender_id=? AND b.status='sealed'
                     AND b.clarification_version < ?""",
                (version, row["published_at"], row["tender_id"], version),
            )
            conn.execute(
                "UPDATE clarifications SET release_version=(SELECT version FROM clarification_versions WHERE clarification_id=?) WHERE id=?",
                (row["id"], row["id"]),
            )

    def _audit(self, conn: sqlite3.Connection, tender_id: int | None, actor: str,
               action: str, details: dict[str, Any], event_key: str | None = None) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO timeline(tender_id,actor,action,details,event_key,created_at) VALUES(?,?,?,?,?,?)",
            (tender_id, actor, action,
             json.dumps(details, ensure_ascii=False, sort_keys=True), event_key, utcnow()),
        )

    def _tender(self, conn: sqlite3.Connection, tender_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM tenders WHERE id=?", (tender_id,)).fetchone()
        if not row:
            raise DomainError("采购项目不存在", 404)
        return row

    def _clarification_hash(self, answer: str, affected_vendor_ids: list[int],
                            requires_resubmission: bool) -> str:
        return canonical_hash({
            "answer": answer,
            "affected_vendor_ids": sorted(affected_vendor_ids),
            "requires_resubmission": requires_resubmission,
        })

    def _normalize_vendor_ids(self, conn: sqlite3.Connection, tender_id: int,
                              affected_vendor_ids: Any) -> list[int]:
        if affected_vendor_ids is None:
            return []
        if not isinstance(affected_vendor_ids, list):
            raise DomainError("受影响供应商必须是编号数组")
        normalized: list[int] = []
        for raw in affected_vendor_ids:
            try:
                vendor_id = int(raw)
            except (TypeError, ValueError) as exc:
                raise DomainError("供应商编号无效") from exc
            if vendor_id not in normalized:
                normalized.append(vendor_id)
        placeholders = ",".join("?" for _ in normalized)
        if normalized:
            found = {
                row["id"]
                for row in conn.execute(
                    f"SELECT id FROM vendors WHERE id IN ({placeholders})", normalized
                ).fetchall()
            }
            missing = sorted(set(normalized) - found)
            if missing:
                raise DomainError("受影响供应商不存在: %s" % ",".join(map(str, missing)), 404)
        return normalized

    def _invalidate_bids(self, conn: sqlite3.Connection, tender_id: int, clarification_version: int,
                         affected_vendor_ids: list[int], requires_resubmission: bool,
                         published_at: str) -> list[int]:
        rows = conn.execute(
            """SELECT id,vendor_id FROM bids
               WHERE tender_id=? AND status='sealed' AND clarification_version<?""",
            (tender_id, clarification_version),
        ).fetchall()
        affected_set = set(affected_vendor_ids)
        bid_ids: list[int] = []
        now = utcnow()
        for bid in rows:
            requires = 1 if (not affected_vendor_ids or bid["vendor_id"] in affected_set) and requires_resubmission else 0
            conn.execute(
                """INSERT OR IGNORE INTO bid_clarification_status
                   (bid_id,tender_id,vendor_id,clarification_version,requires_resubmission,status,created_at)
                   VALUES(?,?,?,?,?,'pending',?)""",
                (bid["id"], tender_id, bid["vendor_id"], clarification_version, requires, published_at),
            )
            conn.execute(
                """INSERT OR IGNORE INTO timeline(tender_id,actor,action,details,event_key,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (
                    tender_id, "system", "clarification.bid_invalidated",
                    json.dumps({"bid_id": bid["id"], "clarification_version": clarification_version,
                                "requires_resubmission": bool(requires)}, ensure_ascii=False,
                               sort_keys=True),
                    f"clarification-{clarification_version}-bid-{bid['id']}", now,
                ),
            )
            bid_ids.append(bid["id"])
        return bid_ids

    def _pending_clarification_count(self, conn: sqlite3.Connection, tender_id: int) -> int:
        return conn.execute(
            """SELECT COUNT(*) AS c
               FROM bid_clarification_status s JOIN bids b ON b.id=s.bid_id
               WHERE s.tender_id=? AND s.status='pending' AND b.status='sealed'""",
            (tender_id,),
        ).fetchone()["c"]

    def create_vendor(self, actor: str, role: str, vendor_no: str, name: str,
                      representative: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "创建供应商")
        if not vendor_no.strip() or not name.strip() or not representative.strip():
            raise DomainError("供应商编号、名称和代表不能为空")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO vendors(vendor_no,name,representative,created_at) VALUES(?,?,?,?)",
                    (vendor_no.strip(), name.strip(), representative.strip(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("供应商编号已存在", 409) from exc
            self._audit(conn, None, actor, "vendor.created", {"vendor_no": vendor_no.strip()})
            return dict(conn.execute("SELECT * FROM vendors WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_tender(self, actor: str, role: str, tender_no: str, title: str,
                      deadline: str, criteria: list[dict[str, Any]], description: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement"}, "创建采购项目")
        parse_time(deadline)
        if not tender_no.strip() or not title.strip():
            raise DomainError("项目编号和标题不能为空")
        normalized_criteria = []
        total_weight = Decimal("0")
        for item in criteria:
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                raise DomainError("评分项格式无效")
            kind = item.get("kind", "direct")
            if kind not in {"direct", "cost"}:
                raise DomainError("评分项类型只支持 direct 或 cost")
            try:
                weight = Decimal(str(item["weight"]))
                max_value = Decimal(str(item.get("max_value", 100)))
            except (KeyError, InvalidOperation) as exc:
                raise DomainError("评分权重或上限无效") from exc
            if weight <= 0 or max_value <= 0:
                raise DomainError("评分权重和上限必须大于0")
            total_weight += weight
            normalized_criteria.append({"name": str(item["name"]).strip(), "kind": kind,
                                        "weight": float(weight), "max_value": float(max_value)})
        if not normalized_criteria or total_weight != 100:
            raise DomainError("评分项权重合计必须等于100")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO tenders(tender_no,title,description,deadline,criteria,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (tender_no.strip(), title.strip(), description.strip(), parse_time(deadline).isoformat(timespec="seconds"),
                     json.dumps(normalized_criteria, ensure_ascii=False), actor, utcnow(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目编号已存在", 409) from exc
            self._audit(conn, cur.lastrowid, actor, "tender.created", {"tender_no": tender_no.strip()})
            return dict(self._tender(conn, cur.lastrowid))

    def publish_tender(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement"}, "发布采购项目")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "draft":
                raise DomainError("只有草稿项目可以发布", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            conn.execute("UPDATE tenders SET status='published',version=version+1,updated_at=? WHERE id=?", (utcnow(), tender_id))
            self._audit(conn, tender_id, actor, "tender.published", {"deadline": tender["deadline"]})
            return dict(self._tender(conn, tender_id))

    def submit_bid(self, actor: str, role: str, tender_id: int, vendor_id: int,
                   payload: dict[str, Any], price: float, expected_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor"}, "提交投标")
        if not isinstance(payload, dict):
            raise DomainError("投标内容必须是对象")
        try:
            price = float(price)
        except (TypeError, ValueError) as exc:
            raise DomainError("报价必须是数值") from exc
        if price <= 0:
            raise DomainError("报价必须大于0")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "published":
                raise DomainError("当前项目不接受投标", 409)
            if datetime.now(timezone.utc) >= parse_time(tender["deadline"]):
                raise DomainError("投标截止时间已过", 409)
            vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
            if not vendor:
                raise DomainError("供应商不存在", 404)
            if not conn.execute("SELECT 1 FROM conflicts WHERE tender_id=? AND vendor_id=? AND evaluator=?", (tender_id, vendor_id, actor)).fetchone():
                pass
            existing = conn.execute(
                "SELECT * FROM bids WHERE tender_id=? AND vendor_id=?", (tender_id, vendor_id)
            ).fetchone()
            now = utcnow()
            pending = []
            if existing:
                pending = conn.execute(
                    "SELECT * FROM bid_clarification_status WHERE bid_id=? AND status='pending' ORDER BY clarification_version",
                    (existing["id"],),
                ).fetchall()
                only_confirmation = pending and not any(row["requires_resubmission"] for row in pending)
                if only_confirmation:
                    raise DomainError("澄清后该投标只需确认，不能直接重提", 409)
                if existing["clarification_version"] < tender["clarification_version"] and not any(
                    row["requires_resubmission"] for row in pending
                ):
                    raise DomainError("投标对应澄清版本已过期，请先确认", 409)
            payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            digest = canonical_hash(payload)
            if existing:
                if existing["status"] != "sealed":
                    raise DomainError("投标已撤回或已开标，不能修改", 409)
                if expected_version is None or existing["version"] != int(expected_version):
                    raise DomainError("投标已变化，请刷新后重试", 409)
                conn.execute(
                    """UPDATE bids
                       SET payload=?,payload_hash=?,price=?,version=version+1,clarification_version=?,submitted_at=?
                       WHERE id=? AND version=?""",
                    (payload_text, digest, price, tender["clarification_version"], now,
                     existing["id"], expected_version),
                )
                if pending:
                    conn.execute(
                        "UPDATE bid_clarification_status SET status='resolved',resolved_at=? WHERE bid_id=? AND status='pending'",
                        (now, existing["id"]),
                    )
                bid_id = existing["id"]
                action = "bid.resubmitted" if pending else "bid.updated"
            else:
                cur = conn.execute(
                    """INSERT INTO bids(tender_id,vendor_id,payload,payload_hash,price,clarification_version,
                                        original_submitted_at,submitted_by,submitted_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (tender_id, vendor_id, payload_text, digest, price,
                     tender["clarification_version"], now, actor, now),
                )
                bid_id = cur.lastrowid
                action = "bid.submitted"
            self._audit(conn, tender_id, actor, action, {"bid_id": bid_id, "vendor_id": vendor_id, "hash": digest})
            bid = dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())
            bid["payload_hash"] = digest
            return bid

    def withdraw_bid(self, actor: str, role: str, bid_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor"}, "撤回投标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if bid["submitted_by"] != actor:
                raise DomainError("只能撤回自己的投标", 403)
            if bid["version"] != int(expected_version):
                raise DomainError("投标已变化，请刷新后重试", 409)
            if datetime.now(timezone.utc) >= parse_time(tender["deadline"]) or bid["status"] != "sealed":
                raise DomainError("截止后不能撤回投标", 409)
            conn.execute("UPDATE bids SET status='withdrawn',version=version+1 WHERE id=?", (bid_id,))
            self._audit(conn, bid["tender_id"], actor, "bid.withdrawn", {"bid_id": bid_id})
            return dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())

    def confirm_bid(self, actor: str, role: str, bid_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor"}, "确认投标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if bid["submitted_by"] != actor:
                raise DomainError("只能确认自己的投标", 403)
            if tender["status"] != "published" or datetime.now(timezone.utc) >= parse_time(tender["deadline"]):
                raise DomainError("当前投标不能确认", 409)
            if bid["status"] != "sealed":
                raise DomainError("只有密封投标可以确认", 409)
            if bid["version"] != int(expected_version):
                raise DomainError("投标已变化，请刷新后重试", 409)
            pending = conn.execute(
                "SELECT * FROM bid_clarification_status WHERE bid_id=? AND status='pending' ORDER BY clarification_version",
                (bid_id,),
            ).fetchall()
            if not pending:
                raise DomainError("没有待确认的澄清版本", 409)
            must_resubmit = [row["clarification_version"] for row in pending if row["requires_resubmission"]]
            if must_resubmit:
                raise DomainError("澄清版本 %s 要求重新提交投标" % ",".join(map(str, must_resubmit)), 409)
            now = utcnow()
            conn.execute(
                "UPDATE bids SET clarification_version=?,version=version+1 WHERE id=? AND version=?",
                (tender["clarification_version"], bid_id, expected_version),
            )
            conn.execute(
                "UPDATE bid_clarification_status SET status='confirmed',resolved_at=? WHERE bid_id=? AND status='pending'",
                (now, bid_id),
            )
            self._audit(conn, bid["tender_id"], actor, "bid.confirmed",
                        {"bid_id": bid_id, "clarification_version": tender["clarification_version"]})
            return dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())

    reconfirm_bid = confirm_bid

    def open_bids(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "开标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "published":
                raise DomainError("项目当前不能开标", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            if datetime.now(timezone.utc) < parse_time(tender["deadline"]):
                raise DomainError("尚未到开标时间", 409)
            pending_count = self._pending_clarification_count(conn, tender_id)
            if pending_count:
                raise DomainError("仍有投标未确认或未按最新澄清重提，不能开标", 409)
            rows = conn.execute(
                "SELECT * FROM bids WHERE tender_id=? AND status='sealed' ORDER BY id", (tender_id,)
            ).fetchall()
            validated = []
            for row in rows:
                digest = canonical_hash(json.loads(row["payload"]))
                if digest != row["payload_hash"]:
                    raise DomainError("投标完整性校验失败: %s" % row["id"], 409)
                if row["clarification_version"] < tender["clarification_version"]:
                    raise DomainError("投标 %s 不是最新澄清版本，不能开标" % row["id"], 409)
                validated.append((row, digest))
            opened = []
            now = utcnow()
            for row, _digest in validated:
                conn.execute("UPDATE bids SET status='opened',opened_at=?,version=version+1 WHERE id=?", (now, row["id"]))
                opened.append(dict(conn.execute("SELECT * FROM bids WHERE id=?", (row["id"],)).fetchone()))
            conn.execute("UPDATE tenders SET status='opened',version=version+1,updated_at=? WHERE id=?", (now, tender_id))
            self._audit(conn, tender_id, actor, "tender.opened", {"bid_count": len(opened)})
            return {"tender": dict(self._tender(conn, tender_id)), "bids": opened}

    def declare_conflict(self, actor: str, role: str, tender_id: int, evaluator: str,
                         vendor_id: int | None, reason: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"evaluator", "procurement", "supervisor"}, "申报利益冲突")
        if not evaluator.strip() or not reason.strip():
            raise DomainError("评审人和冲突原因不能为空")
        with self.connect() as conn:
            self._tender(conn, tender_id)
            try:
                cur = conn.execute(
                    "INSERT INTO conflicts(tender_id,evaluator,vendor_id,reason,declared_by,created_at) VALUES(?,?,?,?,?,?)",
                    (tender_id, evaluator.strip(), vendor_id, reason.strip(), actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("利益冲突已申报", 409) from exc
            self._audit(conn, tender_id, actor, "conflict.declared", {"evaluator": evaluator.strip(), "vendor_id": vendor_id, "reason": reason.strip()})
            return dict(conn.execute("SELECT * FROM conflicts WHERE id=?", (cur.lastrowid,)).fetchone())

    def evaluate_bid(self, actor: str, role: str, bid_id: int, values: dict[str, float],
                     comment: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"evaluator"}, "评分")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if tender["status"] not in {"opened", "reevaluation"} or tender["evaluations_locked"]:
                raise DomainError("当前项目不能评分", 409)
            if bid["status"] not in {"opened", "qualified"}:
                raise DomainError("该投标不能评分", 409)
            conflict = conn.execute(
                "SELECT 1 FROM conflicts WHERE tender_id=? AND evaluator=? AND (vendor_id=? OR vendor_id IS NULL)",
                (tender["id"], actor, bid["vendor_id"]),
            ).fetchone()
            if conflict:
                raise DomainError("评审人与该供应商存在利益冲突", 403)
            criteria = json.loads(tender["criteria"])
            missing = [c["name"] for c in criteria if c["name"] not in values]
            if missing:
                raise DomainError("缺少评分项: " + ",".join(missing))
            created = []
            now = utcnow()
            for criterion in criteria:
                try:
                    raw = float(values[criterion["name"]])
                except (TypeError, ValueError) as exc:
                    raise DomainError("评分值必须是数值") from exc
                if raw < 0 or raw > criterion["max_value"]:
                    raise DomainError("评分值超出范围: " + criterion["name"])
                if criterion["kind"] == "direct":
                    score = raw / criterion["max_value"] * 100
                else:
                    benchmark = criterion["max_value"]
                    score = min(100.0, benchmark / raw * 100) if raw > 0 else 0.0
                existing = conn.execute(
                    """SELECT * FROM evaluations WHERE bid_id=? AND evaluation_round=? AND evaluator=? AND criterion=?""",
                    (bid_id, tender["evaluation_round"], actor, criterion["name"]),
                ).fetchone()
                if existing:
                    raise DomainError("该评分项已提交，不能覆盖", 409)
                cur = conn.execute(
                    """INSERT INTO evaluations(bid_id,evaluation_round,evaluator,criterion,raw_value,score,comment,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (bid_id, tender["evaluation_round"], actor, criterion["name"], raw, score, comment.strip(), now, now),
                )
                created.append(dict(conn.execute("SELECT * FROM evaluations WHERE id=?", (cur.lastrowid,)).fetchone()))
            self._audit(conn, tender["id"], actor, "bid.evaluated", {"bid_id": bid_id, "criteria": [item["criterion"] for item in created]})
            return {"bid_id": bid_id, "evaluator": actor, "round": tender["evaluation_round"], "evaluations": created}

    def disqualify_bid(self, actor: str, role: str, bid_id: int, reason: str,
                       expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "废标")
        if not reason.strip():
            raise DomainError("废标理由不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            if bid["version"] != int(expected_version):
                raise DomainError("投标已变化，请刷新后重试", 409)
            if bid["status"] not in {"opened", "qualified"}:
                raise DomainError("当前投标不能废标", 409)
            conn.execute("UPDATE bids SET status='disqualified',version=version+1 WHERE id=?", (bid_id,))
            self._audit(conn, bid["tender_id"], actor, "bid.disqualified", {"bid_id": bid_id, "reason": reason.strip()})
            return dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())

    def ask_clarification(self, actor: str, role: str, tender_id: int, vendor_id: int, question: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor", "procurement", "supervisor"}, "提交澄清")
        if not question.strip():
            raise DomainError("澄清问题不能为空")
        with self.connect() as conn:
            self._tender(conn, tender_id)
            cur = conn.execute(
                "INSERT INTO clarifications(tender_id,vendor_id,question,created_at) VALUES(?,?,?,?)",
                (tender_id, vendor_id, question.strip(), utcnow()),
            )
            self._audit(conn, tender_id, actor, "clarification.asked", {"clarification_id": cur.lastrowid})
            return dict(conn.execute("SELECT * FROM clarifications WHERE id=?", (cur.lastrowid,)).fetchone())

    def _publish_clarification(self, conn: sqlite3.Connection, actor: str, tender_id: int,
                               clarification_no: str, answer: str,
                               affected_vendor_ids: list[int] | None = None,
                               requires_resubmission: bool = True,
                               expected_version: int | None = None,
                               clarification_id: int | None = None,
                               question: str = "", vendor_id: int | None = None) -> dict[str, Any]:
        tender = self._tender(conn, tender_id)
        normalized_answer = answer.strip()
        normalized_no = clarification_no.strip()
        affected = self._normalize_vendor_ids(conn, tender_id, affected_vendor_ids)
        digest = self._clarification_hash(normalized_answer, affected, requires_resubmission)
        existing_version = conn.execute(
            "SELECT * FROM clarification_versions WHERE clarification_no=?", (normalized_no,)
        ).fetchone()
        existing_clarification = None
        if clarification_id is not None:
            existing_clarification = conn.execute(
                "SELECT * FROM clarifications WHERE id=?", (clarification_id,)
            ).fetchone()
        else:
            existing_clarification = conn.execute(
                "SELECT * FROM clarifications WHERE clarification_no=? AND release_version IS NULL AND status='pending'",
                (normalized_no,),
            ).fetchone()
        if existing_version:
            if existing_version["tender_id"] != tender_id or existing_version["content_hash"] != digest:
                raise DomainError("澄清编号已存在且不可修订；如已变更请使用新编号", 409)
            existing_clarification = conn.execute(
                "SELECT * FROM clarifications WHERE id=?", (existing_version["clarification_id"],)
            ).fetchone()
        elif existing_clarification:
            if existing_clarification["tender_id"] != tender_id:
                raise DomainError("澄清不存在", 404)
            if clarification_id is None and existing_clarification["clarification_no"] != normalized_no:
                raise DomainError("澄清编号与既有记录不一致", 409)
            stored_hash = existing_clarification["content_hash"]
            if stored_hash and stored_hash != digest:
                raise DomainError("澄清编号已存在且不可修订；如已变更请使用新编号", 409)
        recovering = bool(existing_version) or (
            existing_clarification is not None and existing_clarification["release_version"] is not None
        )
        if not recovering and expected_version is not None and tender["version"] != int(expected_version):
            raise DomainError("项目版本冲突，请刷新后重试", 409)
        if tender["status"] != "published":
            raise DomainError("只有投标中的项目可以发布澄清", 409)
        if datetime.now(timezone.utc) >= parse_time(tender["deadline"]):
            raise DomainError("投标截止后不能发布澄清", 409)
        replayed = False
        repaired = False
        if existing_version:
            replayed = True
            new_version = int(existing_version["version"])
            published_at = existing_version["published_at"]
        elif existing_clarification is not None and existing_clarification["release_version"] is not None:
            replayed = True
            new_version = max(
                int(existing_clarification["release_version"]),
                tender["clarification_version"] + 1,
            )
            published_at = existing_clarification["answered_at"] or utcnow()
        else:
            new_version = max(tender["clarification_version"] + 1, 2)
            published_at = utcnow()
        if existing_clarification is None:
            try:
                cur = conn.execute(
                    """INSERT INTO clarifications(tender_id,vendor_id,clarification_no,question,answer,
                                                  content_hash,status,release_version,answered_by,created_at,answered_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (tender_id, vendor_id, normalized_no, question, normalized_answer, digest,
                     "published", new_version, actor, published_at, published_at),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("澄清编号已存在且不可修订；如已变更请使用新编号", 409) from exc
            clarification_id = cur.lastrowid
            if recovering:
                repaired = True
        else:
            clarification_id = existing_clarification["id"]
            stored_publisher = existing_clarification["answered_by"] or actor
        if not existing_version:
            conn.execute(
                """INSERT OR IGNORE INTO clarification_versions(tender_id,clarification_id,clarification_no,version,answer,
                                                      affected_vendor_ids,requires_resubmission,content_hash,
                                                      published_by,published_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (tender_id, clarification_id, normalized_no, new_version, normalized_answer,
                 json.dumps(affected, ensure_ascii=False), 1 if requires_resubmission else 0, digest,
                 actor, published_at),
            )
            if recovering:
                repaired = True
        elif existing_version["clarification_id"] != clarification_id:
            conn.execute(
                "UPDATE clarification_versions SET clarification_id=? WHERE id=?",
                (clarification_id, existing_version["id"]),
            )
            repaired = True
        if existing_clarification is not None:
            conn.execute(
                """UPDATE clarifications
                   SET clarification_no=?,answer=COALESCE(answer,?),content_hash=COALESCE(content_hash,?),
                       status='published',release_version=?,answered_by=COALESCE(answered_by,?),answered_at=?
                   WHERE id=?""",
                (normalized_no, normalized_answer, digest, new_version, stored_publisher,
                 published_at, clarification_id),
            )
            if recovering and (
                existing_clarification["release_version"] is None
                or existing_clarification["status"] != "published"
                or existing_clarification["content_hash"] is None
            ):
                repaired = True
        if tender["clarification_version"] < new_version:
            conn.execute(
                """UPDATE tenders
                   SET clarification_version=?,version=MAX(version, ?)+1,updated_at=?
                   WHERE id=?""",
                (new_version, new_version, published_at if not replayed else utcnow(), tender_id),
            )
            if replayed:
                repaired = True
        root_event_key = f"clarification-published-{normalized_no}"
        root_missing = conn.execute(
            "SELECT 1 FROM timeline WHERE event_key=?", (root_event_key,)
        ).fetchone() is None
        if root_missing and recovering:
            repaired = True
        before_status = {
            (row["bid_id"], row["clarification_version"]): row["status"]
            for row in conn.execute(
                "SELECT bid_id,clarification_version,status FROM bid_clarification_status WHERE clarification_version=?",
                (new_version,),
            ).fetchall()
        }
        eligible_rows = conn.execute(
            """SELECT id FROM bids
               WHERE tender_id=? AND status='sealed' AND clarification_version<?""",
            (tender_id, new_version),
        ).fetchall()
        missing_invalidation_audit = [
            row["id"]
            for row in eligible_rows
            if not conn.execute(
                "SELECT 1 FROM timeline WHERE event_key=?",
                (f"clarification-{new_version}-bid-{row['id']}",),
            ).fetchone()
        ]
        affected_bid_ids = self._invalidate_bids(
            conn, tender_id, new_version, affected, requires_resubmission, published_at
        )
        after_rows = conn.execute(
            "SELECT bid_id,status FROM bid_clarification_status WHERE clarification_version=?",
            (new_version,),
        ).fetchall()
        status_repaired = any(
            before_status.get((row["bid_id"], new_version)) != row["status"]
            for row in after_rows
        )
        repaired = repaired or status_repaired or bool(missing_invalidation_audit) or (root_missing and recovering)
        clarification = conn.execute(
            "SELECT * FROM clarifications WHERE id=?", (existing_version["clarification_id"] if existing_version else clarification_id,),
        ).fetchone()
        version = dict(conn.execute(
            "SELECT * FROM clarification_versions WHERE clarification_no=?", (normalized_no,)
        ).fetchone())
        version["affected_vendor_ids"] = json.loads(version["affected_vendor_ids"])
        version["requires_resubmission"] = bool(version["requires_resubmission"])
        self._audit(
            conn, tender_id, actor, "clarification.published",
            {"clarification_no": normalized_no, "version": new_version,
             "affected_bid_ids": affected_bid_ids, "replayed": replayed, "repaired": repaired},
            f"clarification-published-{normalized_no}",
        )
        result = {"clarification": dict(clarification), "version": version,
                  "affected_bid_ids": affected_bid_ids, "replayed": replayed,
                  "repaired": repaired, "tender": dict(self._tender(conn, tender_id))}
        return result

    def publish_clarification(self, actor: str, role: str, tender_id: int, clarification_no: str,
                              answer: str, affected_vendor_ids: list[int] | None = None,
                              requires_resubmission: bool = True,
                              expected_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "发布澄清")
        if not clarification_no.strip():
            raise DomainError("澄清编号不能为空")
        if not answer.strip():
            raise DomainError("澄清答复不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._publish_clarification(
                conn, actor, tender_id, clarification_no, answer, affected_vendor_ids,
                requires_resubmission, expected_version,
            )

    def answer_clarification(self, actor: str, role: str, clarification_id: int,
                             answer: str, publish: bool = True, clarification_no: str | None = None,
                             affected_vendor_ids: list[int] | None = None,
                             requires_resubmission: bool = True,
                             expected_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "答复澄清")
        if not answer.strip():
            raise DomainError("澄清答复不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM clarifications WHERE id=?", (clarification_id,)).fetchone()
            if not row:
                raise DomainError("澄清不存在", 404)
            if publish:
                number = clarification_no or row["clarification_no"] or "CL-%s" % clarification_id
                return self._publish_clarification(
                    conn, actor, row["tender_id"], number, answer, affected_vendor_ids,
                    requires_resubmission, expected_version, clarification_id=clarification_id,
                    question=row["question"], vendor_id=row["vendor_id"],
                )
            if row["status"] != "pending":
                raise DomainError("澄清已经处理", 409)
            conn.execute(
                "UPDATE clarifications SET answer=?,status='answered',answered_by=?,answered_at=? WHERE id=?",
                (answer.strip(), actor, utcnow(), clarification_id),
            )
            self._audit(conn, row["tender_id"], actor, "clarification.answered",
                        {"clarification_id": clarification_id, "published": False})
            return dict(conn.execute("SELECT * FROM clarifications WHERE id=?", (clarification_id,)).fetchone())

    def submit_complaint(self, actor: str, role: str, tender_id: int, body: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor", "evaluator", "procurement", "supervisor"}, "提交投诉")
        if not body.strip():
            raise DomainError("投诉内容不能为空")
        with self.connect() as conn:
            tender = self._tender(conn, tender_id)
            if tender["status"] in {"awarded", "cancelled"}:
                raise DomainError("项目已经结束，不能提交投诉", 409)
            cur = conn.execute(
                "INSERT INTO complaints(tender_id,complainant,body,created_at) VALUES(?,?,?,?)",
                (tender_id, actor, body.strip(), utcnow()),
            )
            self._audit(conn, tender_id, actor, "complaint.submitted", {"complaint_id": cur.lastrowid})
            return dict(conn.execute("SELECT * FROM complaints WHERE id=?", (cur.lastrowid,)).fetchone())

    def resolve_complaint(self, actor: str, role: str, complaint_id: int, decision: str,
                          resolution: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "处理投诉")
        if decision not in {"accepted", "rejected"} or not resolution.strip():
            raise DomainError("投诉决定或处理说明无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            complaint = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
            if not complaint:
                raise DomainError("投诉不存在", 404)
            if complaint["status"] != "open":
                raise DomainError("投诉已经处理", 409)
            conn.execute(
                "UPDATE complaints SET status=?,resolution=?,reviewed_by=?,resolved_at=? WHERE id=?",
                (decision, resolution.strip(), actor, utcnow(), complaint_id),
            )
            if decision == "accepted":
                tender = self._tender(conn, complaint["tender_id"])
                if tender["status"] in {"awarded", "cancelled"}:
                    raise DomainError("已结束项目不能重新评审", 409)
                conn.execute(
                    "UPDATE tenders SET status='reevaluation',evaluation_round=evaluation_round+1,evaluations_locked=0,version=version+1,updated_at=? WHERE id=?",
                    (utcnow(), tender["id"]),
                )
            self._audit(conn, complaint["tender_id"], actor, "complaint.resolved", {"complaint_id": complaint_id, "decision": decision})
            return dict(conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone())

    def award_tender(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "授标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] not in {"opened", "reevaluation"}:
                raise DomainError("当前项目不能授标", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            open_complaint = conn.execute("SELECT COUNT(*) AS c FROM complaints WHERE tender_id=? AND status='open'", (tender_id,)).fetchone()["c"]
            if open_complaint:
                raise DomainError("存在未处理投诉，不能授标", 409)
            bids = conn.execute("SELECT * FROM bids WHERE tender_id=? AND status IN ('opened','qualified')", (tender_id,)).fetchall()
            criteria = json.loads(tender["criteria"])
            expected_criteria = {c["name"] for c in criteria}
            ranking = []
            for bid in bids:
                rows = conn.execute(
                    "SELECT criterion,AVG(score) AS score FROM evaluations WHERE bid_id=? AND evaluation_round=? GROUP BY criterion",
                    (bid["id"], tender["evaluation_round"]),
                ).fetchall()
                scores = {row["criterion"]: row["score"] for row in rows}
                if set(scores) != expected_criteria:
                    raise DomainError("投标尚未完成全部评分: %s" % bid["id"], 409)
                weighted = 0.0
                for criterion in criteria:
                    weighted += scores[criterion["name"]] * criterion["weight"] / 100
                ranking.append({"bid_id": bid["id"], "vendor_id": bid["vendor_id"], "price": bid["price"], "score": round(weighted, 2)})
            if not ranking:
                raise DomainError("没有可授标的有效投标", 409)
            ranking.sort(key=lambda item: (-item["score"], item["price"], item["bid_id"]))
            winner = ranking[0]
            snapshot = {"tender_id": tender_id, "round": tender["evaluation_round"], "ranking": ranking, "winner": winner, "awarded_by": actor, "awarded_at": utcnow()}
            conn.execute(
                "UPDATE tenders SET status='awarded',awarded_bid_id=?,award_snapshot=?,evaluations_locked=1,version=version+1,updated_at=? WHERE id=? AND version=?",
                (winner["bid_id"], json.dumps(snapshot, ensure_ascii=False), utcnow(), tender_id, expected_version),
            )
            conn.execute("UPDATE bids SET status='awarded',version=version+1 WHERE id=?", (winner["bid_id"],))
            self._audit(conn, tender_id, actor, "tender.awarded", {"winner": winner, "ranking": ranking})
            return {"tender": dict(self._tender(conn, tender_id)), "award": snapshot}

    def _bid_view(self, conn: sqlite3.Connection, bid: sqlite3.Row, actor: str, role: str,
                  tender: sqlite3.Row | None = None) -> dict[str, Any]:
        tender = tender or self._tender(conn, bid["tender_id"])
        item = dict(bid)
        is_owner = role == "vendor" and bid["submitted_by"] == actor
        is_manager = role in {"procurement", "supervisor", "auditor"}
        is_evaluator = role == "evaluator"
        if not (is_owner or is_manager or is_evaluator):
            raise DomainError("无权查看该投标", 403)
        if role == "vendor" and not is_owner:
            raise DomainError("无权查看其他供应商的投标", 403)
        opened = tender["status"] in {"opened", "reevaluation", "awarded"}
        if not opened:
            item.pop("payload", None)
        statuses = [dict(r) for r in conn.execute(
            "SELECT * FROM bid_clarification_status WHERE bid_id=? ORDER BY clarification_version",
            (bid["id"],),
        ).fetchall()]
        for status in statuses:
            status["requires_resubmission"] = bool(status["requires_resubmission"])
        item["clarification_status"] = statuses
        return item

    def get_bid(self, actor: str, role: str, bid_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            return self._bid_view(conn, bid, actor, role, tender)

    def get_tender(self, actor: str, role: str, tender_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            tender = dict(self._tender(conn, tender_id))
            bids = []
            if role in {"procurement", "supervisor", "auditor"}:
                rows = conn.execute("SELECT * FROM bids WHERE tender_id=? ORDER BY id", (tender_id,)).fetchall()
                bids = []
                opened = tender["status"] in {"opened", "reevaluation", "awarded"}
                for row in rows:
                    item = dict(row)
                    if not opened:
                        item.pop("payload", None)
                    item["clarification_status"] = [
                        dict(status, requires_resubmission=bool(status["requires_resubmission"]))
                        for status in conn.execute(
                            "SELECT * FROM bid_clarification_status WHERE bid_id=? ORDER BY clarification_version",
                            (row["id"],),
                        ).fetchall()
                    ]
                    bids.append(item)
            elif role == "vendor":
                bids = [
                    self._bid_view(conn, row, actor, role, conn.execute(
                        "SELECT * FROM tenders WHERE id=?", (tender_id,)
                    ).fetchone())
                    for row in conn.execute(
                        "SELECT * FROM bids WHERE tender_id=? AND submitted_by=? ORDER BY id",
                        (tender_id, actor),
                    ).fetchall()
                ]
            else:
                bids = [dict(r) for r in conn.execute(
                    """SELECT id,tender_id,vendor_id,price,status,version,clarification_version,payload_hash,
                             original_submitted_at,submitted_at,opened_at
                      FROM bids WHERE tender_id=? ORDER BY id""",
                    (tender_id,),
                ).fetchall()]
            clarifications = [dict(r) for r in conn.execute(
                """SELECT c.id,c.tender_id,c.vendor_id,c.clarification_no,c.question,c.answer,c.status,
                          c.release_version,c.answered_at,v.version,v.affected_vendor_ids,v.requires_resubmission,
                          v.published_at
                   FROM clarifications c JOIN clarification_versions v ON v.clarification_id=c.id
                   WHERE c.tender_id=? AND c.status='published' ORDER BY v.version""",
                (tender_id,),
            ).fetchall()]
            for item in clarifications:
                item["affected_vendor_ids"] = json.loads(item["affected_vendor_ids"])
                item["requires_resubmission"] = bool(item["requires_resubmission"])
                if role not in {"procurement", "supervisor", "auditor"}:
                    item.pop("affected_vendor_ids", None)
            return {"tender": tender, "bids": bids, "clarifications": clarifications}

    def state(self, actor: str = "", role: str = "public") -> dict[str, Any]:
        with self.connect() as conn:
            tenders = [dict(r) for r in conn.execute(
                "SELECT id,tender_no,title,description,status,deadline,evaluation_round,version,clarification_version,awarded_bid_id,created_at,updated_at FROM tenders ORDER BY id DESC"
            ).fetchall()]
            timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline ORDER BY id DESC LIMIT 200").fetchall()]
            if role in {"procurement", "supervisor", "auditor"}:
                bids = [dict(r) for r in conn.execute(
                    """SELECT b.id,b.tender_id,b.vendor_id,b.price,b.status,b.version,b.clarification_version,
                              b.payload_hash,b.original_submitted_at,b.submitted_at,b.opened_at,
                              CASE WHEN t.status IN ('opened','reevaluation','awarded') THEN b.payload ELSE NULL END AS payload
                       FROM bids b JOIN tenders t ON t.id=b.tender_id ORDER BY b.id DESC LIMIT 200"""
                ).fetchall()]
                complaints = [dict(r) for r in conn.execute("SELECT * FROM complaints ORDER BY id DESC LIMIT 100").fetchall()]
            elif role == "vendor":
                bids = []
                for row in conn.execute(
                    """SELECT b.*,t.status AS tender_status FROM bids b JOIN tenders t ON t.id=b.tender_id
                       WHERE b.submitted_by=? ORDER BY b.id DESC LIMIT 100""",
                    (actor,),
                ).fetchall():
                    item = dict(row)
                    status = item.pop("tender_status")
                    if status not in {"opened", "reevaluation", "awarded"}:
                        item.pop("payload", None)
                    bids.append(item)
                complaints = [dict(r) for r in conn.execute(
                    "SELECT * FROM complaints WHERE complainant=? ORDER BY id DESC LIMIT 100", (actor,)
                ).fetchall()]
            else:
                bids, complaints = [], []
        return {"tenders": tenders, "bids": bids, "complaints": complaints, "timeline": timeline, "role": role}

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM tenders").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        vendor = self.create_vendor("proc-demo", "procurement", "V-001", "启明科技", "vendor-demo")
        deadline = (datetime.now(timezone.utc) + __import__("datetime").timedelta(hours=1)).isoformat(timespec="seconds")
        tender = self.create_tender(
            "proc-demo", "procurement", "TENDER-DEMO", "服务器采购", deadline,
            [{"name": "价格", "weight": 60, "kind": "cost", "max_value": 1000000},
             {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100}],
        )
        published = self.publish_tender("proc-demo", "procurement", tender["id"], tender["version"])
        self.submit_bid("vendor-demo", "vendor", tender["id"], vendor["id"], {"价格": 900000, "质量": 90}, 900000)
        return {"seeded": True, "tender_id": tender["id"], "vendor_id": vendor["id"], "published_version": published["version"]}


class ApiHandler(BaseHTTPRequestHandler):
    service: ProcurementService

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "public")

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise DomainError("请求体过大", 413)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON 请求体必须是对象")
        return value

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            actor, role = self._headers()
            if path == "/health":
                self._send(200, {"status": "ok", "service": "public-procurement"})
            elif path == "/api/state":
                self._send(200, self.service.state(actor, role))
            elif path.startswith("/api/bids/"):
                self._send(200, self.service.get_bid(actor, role, int(path.split("/")[3])))
            elif path.startswith("/api/tenders/"):
                self._send(200, self.service.get_tender(actor, role, int(path.split("/")[3])))
            else:
                self._send(404, {"error": "接口不存在"})
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (ValueError, IndexError) as exc:
            self._send(400, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path, data, (actor, role) = urlparse(self.path).path, self._json(), self._headers()
            if path == "/api/vendors":
                result = self.service.create_vendor(actor, role, **data)
            elif path == "/api/tenders":
                result = self.service.create_tender(actor, role, **data)
            elif path == "/api/tenders/publish":
                result = self.service.publish_tender(actor, role, **data)
            elif path == "/api/bids":
                result = self.service.submit_bid(actor, role, **data)
            elif path in {"/api/bids/confirm", "/api/bids/reconfirm"}:
                result = self.service.confirm_bid(actor, role, **data)
            elif path == "/api/bids/withdraw":
                result = self.service.withdraw_bid(actor, role, **data)
            elif path == "/api/tenders/open":
                result = self.service.open_bids(actor, role, **data)
            elif path == "/api/conflicts":
                result = self.service.declare_conflict(actor, role, **data)
            elif path == "/api/evaluations":
                result = self.service.evaluate_bid(actor, role, **data)
            elif path == "/api/bids/disqualify":
                result = self.service.disqualify_bid(actor, role, **data)
            elif path.startswith("/api/bids/"):
                raise DomainError("接口不存在", 404)
            elif path == "/api/clarifications":
                result = self.service.ask_clarification(actor, role, **data)
            elif path == "/api/clarifications/publish":
                result = self.service.publish_clarification(actor, role, **data)
            elif path == "/api/clarifications/answer":
                result = self.service.answer_clarification(actor, role, **data)
            elif path == "/api/complaints":
                result = self.service.submit_complaint(actor, role, **data)
            elif path == "/api/complaints/resolve":
                result = self.service.resolve_complaint(actor, role, **data)
            elif path == "/api/tenders/award":
                result = self.service.award_tender(actor, role, **data)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(service: ProcurementService, host: str, port: int) -> None:
    ApiHandler.service = service
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print("Public procurement service listening on http://%s:%s" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="公共采购密封投标服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8209)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = ProcurementService(args.db)
    if args.init:
        print(json.dumps(service.seed_demo() if args.seed else {"initialized": True, "db": args.db}, ensure_ascii=False))
        return
    serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
