#!/usr/bin/env python3
"""Pharmaceutical batch deviation, rework and release decision service."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DB_PATH = Path(__file__).with_name("data.db")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def after_now(value: str | None = None) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) > datetime.now(timezone.utc)
    except ValueError:
        return False


def j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message); self.status, self.message = status, message


class Store:
    def __init__(self, path: str | Path = DB_PATH):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False); self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON"); self.conn.execute("PRAGMA journal_mode=WAL"); self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS factories (
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, country TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS batches (
          id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER NOT NULL REFERENCES factories(id),
          batch_no TEXT NOT NULL, product TEXT NOT NULL, mfg_date TEXT NOT NULL, expiry_date TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('manufactured','investigation','awaiting_resample','conditional','released','rejected','recall_review','recalled')),
          revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(factory_id,batch_no)
        );
        CREATE TABLE IF NOT EXISTS deviations (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id),
          severity TEXT NOT NULL CHECK(severity IN ('critical','minor')), title TEXT NOT NULL, due_at TEXT,
          status TEXT NOT NULL CHECK(status IN ('open','closed')), corrective_action TEXT,
          exception_reason TEXT, exception_until TEXT, exception_approved_by TEXT,
          closed_by TEXT, closed_at TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tests (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id),
          test_type TEXT NOT NULL, result REAL NOT NULL, spec_min REAL NOT NULL, spec_max REAL NOT NULL,
          passed INTEGER NOT NULL, round INTEGER NOT NULL DEFAULT 1, recorded_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rework (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id),
          description TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('planned','completed')),
          created_by TEXT NOT NULL, created_at TEXT NOT NULL, completed_by TEXT, completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS supplier_changes (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id),
          supplier TEXT NOT NULL, change_type TEXT NOT NULL, description TEXT NOT NULL,
          recorded_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stability (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id),
          condition TEXT NOT NULL, timepoint TEXT NOT NULL, result REAL NOT NULL, spec_limit REAL NOT NULL,
          passed INTEGER NOT NULL, recorded_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS decisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id), revision INTEGER NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('release','reject','conditional','resample')), rationale TEXT NOT NULL,
          exception_code TEXT, decided_by TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(batch_id,revision)
        );
        CREATE TABLE IF NOT EXISTS quality_summaries (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id),
          version INTEGER NOT NULL, source TEXT NOT NULL CHECK(source IN ('release','supplement','recall_review')),
          snapshot_json TEXT NOT NULL, note TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(batch_id,version)
        );
        CREATE TABLE IF NOT EXISTS post_release_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id),
          data_type TEXT NOT NULL CHECK(data_type IN ('deviation','test','stability')), payload_json TEXT NOT NULL,
          severity TEXT, passed INTEGER,
          status TEXT NOT NULL CHECK(status IN ('pending','accepted','recall_review','closed')),
          assessment TEXT, assessed_by TEXT, assessed_at TEXT,
          conclusion TEXT, reviewed_by TEXT, reviewed_at TEXT,
          created_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
          entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL
        );
        """)
        self.conn.commit()

    def audit(self, actor: str, action: str, entity_type: str, entity_id: object, details: dict) -> None:
        self.conn.execute("INSERT INTO audit_log(at,actor,action,entity_type,entity_id,details_json) VALUES(?,?,?,?,?,?)",
                          (now(), actor, action, entity_type, str(entity_id), j(details)))

    def close(self) -> None:
        self.conn.close()


class BatchService:
    def __init__(self, store: Store): self.store, self.conn = store, store.conn

    @staticmethod
    def _actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor: raise ApiError(401, "缺少身份")
        if role not in allowed: raise ApiError(403, "角色无权执行此操作")
        return actor

    def _row(self, table: str, identity: int) -> sqlite3.Row:
        row = self.conn.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
        if not row: raise ApiError(404, "对象不存在")
        return row

    def _factory_check(self, actor: str, factory_id: int, batch: sqlite3.Row | None = None) -> None:
        factory = self.conn.execute("SELECT * FROM factories WHERE id=?", (factory_id,)).fetchone()
        if not factory: raise ApiError(404, "工厂不存在")
        if batch is not None and int(batch["factory_id"]) != int(factory_id):
            raise ApiError(403, "不能修改其他工厂的批次")

    def register_factory(self, actor: str | None, role: str | None, code: str, name: str, country: str) -> dict:
        actor = self._actor(actor, role, {"qa"})
        if not code or not name: raise ApiError(400, "工厂代号和名称不能为空")
        try:
            with self.conn:
                cur = self.conn.execute("INSERT INTO factories(code,name,country) VALUES(?,?,?)", (code, name, country))
                self.store.audit(actor, "factory.register", "factory", cur.lastrowid, {"code": code})
        except sqlite3.IntegrityError as exc: raise ApiError(409, "工厂代号已存在") from exc
        return {"id": cur.lastrowid, "code": code, "name": name, "country": country}

    def create_batch(self, actor: str | None, role: str | None, factory_id: int, batch_no: str, product: str, mfg_date: str, expiry_date: str) -> dict:
        actor = self._actor(actor, role, {"operator"})
        self._factory_check(actor, factory_id)
        if not batch_no.strip() or not product.strip() or expiry_date <= mfg_date: raise ApiError(400, "批号、产品或有效期不合法")
        stamp = now()
        try:
            with self.conn:
                cur = self.conn.execute("""INSERT INTO batches(factory_id,batch_no,product,mfg_date,expiry_date,state,created_by,created_at,updated_at)
                                         VALUES(?,?,?,?,?, 'manufactured',?,?,?)""",
                                        (factory_id, batch_no, product, mfg_date, expiry_date, actor, stamp, stamp))
                self.store.audit(actor, "batch.create", "batch", cur.lastrowid, {"factory_id": factory_id, "batch_no": batch_no})
        except sqlite3.IntegrityError as exc: raise ApiError(409, "该工厂批号已存在") from exc
        return self._batch_dict(self._row("batches", cur.lastrowid))

    def add_deviation(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, severity: str, title: str, due_at: str | None, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"operator", "inspector"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        if severity not in {"critical", "minor"} or not title.strip(): raise ApiError(400, "偏差等级或描述不合法")
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "已终态批次不能新增偏差")
        with self.conn:
            cur = self.conn.execute("""INSERT INTO deviations(batch_id,severity,title,due_at,status,created_by,created_at)
                                     VALUES(?,?,?,?,'open',?,?)""", (batch_id, severity, title, due_at, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation")
            self.store.audit(actor, "deviation.open", "deviation", cur.lastrowid, {"batch_id": batch_id, "severity": severity})
        return self._deviation_dict(self._row("deviations", cur.lastrowid))

    def close_deviation(self, actor: str | None, role: str | None, deviation_id: int, corrective_action: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"qa"})
        deviation = self._row("deviations", deviation_id); batch = self._row("batches", deviation["batch_id"])
        if deviation["status"] != "open": raise ApiError(409, "偏差已经关闭")
        if not corrective_action.strip(): raise ApiError(400, "必须填写纠正措施")
        with self.conn:
            self.conn.execute("UPDATE deviations SET status='closed',corrective_action=?,closed_by=?,closed_at=? WHERE id=? AND status='open'",
                              (corrective_action, actor, now(), deviation_id))
            self._advance_batch(batch["id"], expected_revision, "investigation")
            self.store.audit(actor, "deviation.close", "deviation", deviation_id, {"batch_id": batch["id"], "corrective_action": corrective_action})
        return self._deviation_dict(self._row("deviations", deviation_id))

    def approve_exception(self, actor: str | None, role: str | None, deviation_id: int, reason: str, until: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"qa"})
        deviation = self._row("deviations", deviation_id); batch = self._row("batches", deviation["batch_id"])
        if deviation["severity"] == "critical": raise ApiError(409, "关键偏差不允许例外批准")
        if deviation["status"] != "open" or not reason.strip() or not after_now(until): raise ApiError(400, "例外原因或有效期不合法")
        with self.conn:
            self.conn.execute("UPDATE deviations SET exception_reason=?,exception_until=?,exception_approved_by=? WHERE id=?", (reason, until, actor, deviation_id))
            self._advance_batch(batch["id"], expected_revision, batch["state"])
            self.store.audit(actor, "deviation.exception", "deviation", deviation_id, {"batch_id": batch["id"], "reason": reason, "until": until})
        return self._deviation_dict(self._row("deviations", deviation_id))

    def record_test(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, test_type: str, result: float, spec_min: float, spec_max: float, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"lab"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        if not test_type.strip() or spec_min > spec_max: raise ApiError(400, "检验项目或标准不合法")
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "终态批次不能补录检验")
        round_no = self.conn.execute("SELECT COALESCE(MAX(round),0)+1 FROM tests WHERE batch_id=? AND test_type=?", (batch_id, test_type)).fetchone()[0]
        passed = int(spec_min <= result <= spec_max)
        with self.conn:
            cur = self.conn.execute("""INSERT INTO tests(batch_id,test_type,result,spec_min,spec_max,passed,round,recorded_by,created_at)
                                     VALUES(?,?,?,?,?,?,?,?,?)""", (batch_id, test_type, result, spec_min, spec_max, passed, round_no, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation" if (batch["state"] == "awaiting_resample" or not passed) else batch["state"])
            self.store.audit(actor, "test.record", "batch", batch_id, {"test_type": test_type, "result": result, "passed": bool(passed), "round": round_no})
        return self._test_dict(self._row("tests", cur.lastrowid))

    def plan_rework(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, description: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"operator"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "终态批次不能返工")
        with self.conn:
            cur = self.conn.execute("INSERT INTO rework(batch_id,description,status,created_by,created_at) VALUES(?,?,'planned',?,?)", (batch_id, description, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation")
            self.store.audit(actor, "rework.plan", "rework", cur.lastrowid, {"batch_id": batch_id, "description": description})
        return dict(self._row("rework", cur.lastrowid))

    def complete_rework(self, actor: str | None, role: str | None, factory_id: int, rework_id: int, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"operator"})
        row = self._row("rework", rework_id); batch = self._row("batches", row["batch_id"]); self._factory_check(actor, factory_id, batch)
        if row["status"] != "planned": raise ApiError(409, "返工记录已经完成")
        with self.conn:
            self.conn.execute("UPDATE rework SET status='completed',completed_by=?,completed_at=? WHERE id=?", (actor, now(), rework_id))
            self._advance_batch(batch["id"], expected_revision, "investigation")
            self.store.audit(actor, "rework.complete", "rework", rework_id, {"batch_id": batch["id"]})
        return dict(self._row("rework", rework_id))

    def record_supplier_change(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, supplier: str, change_type: str, description: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"operator", "qa"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        with self.conn:
            cur = self.conn.execute("INSERT INTO supplier_changes(batch_id,supplier,change_type,description,recorded_by,created_at) VALUES(?,?,?,?,?,?)",
                                    (batch_id, supplier, change_type, description, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation")
            self.store.audit(actor, "supplier_change.record", "batch", batch_id, {"supplier": supplier, "change_type": change_type})
        return dict(self._row("supplier_changes", cur.lastrowid))

    def record_stability(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, condition: str, timepoint: str, result: float, spec_limit: float, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"lab"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        passed = int(result <= spec_limit)
        with self.conn:
            cur = self.conn.execute("INSERT INTO stability(batch_id,condition,timepoint,result,spec_limit,passed,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                    (batch_id, condition, timepoint, result, spec_limit, passed, actor, now()))
            self._advance_batch(batch_id, expected_revision, batch["state"])
            self.store.audit(actor, "stability.record", "batch", batch_id, {"condition": condition, "timepoint": timepoint, "passed": bool(passed)})
        return dict(self._row("stability", cur.lastrowid))

    def decide(self, actor: str | None, role: str | None, batch_id: int, decision: str, rationale: str, expected_revision: int, exception_code: str = "") -> dict:
        actor = self._actor(actor, role, {"qa"})
        batch = self._row("batches", batch_id)
        if decision not in {"release", "reject", "conditional", "resample"}: raise ApiError(400, "放行决定不合法")
        if batch["state"] in {"released", "rejected", "recalled"}: raise ApiError(409, "批次已经是终态")
        if batch["state"] == "recall_review": raise ApiError(409, "批次处于召回待审，请先完成复核")
        if int(expected_revision) != int(batch["revision"]): raise ApiError(409, "批次已被其他工厂或质量人员修改，请刷新版本")
        if not rationale.strip(): raise ApiError(400, "必须填写决定依据")
        deviations = self.conn.execute("SELECT * FROM deviations WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        open_deviations = [d for d in deviations if d["status"] == "open"]
        latest_tests: dict[str, sqlite3.Row] = {}
        for row in self.conn.execute("SELECT * FROM tests WHERE batch_id=? ORDER BY id", (batch_id,)):
            latest_tests[row["test_type"]] = row
        if decision in {"release", "conditional"} and not latest_tests:
            raise ApiError(409, "放行前至少需要一项检验结果")
        if decision in {"release", "conditional"} and any(not row["passed"] for row in latest_tests.values()):
            raise ApiError(409, "最新检验结果仍有不合格项")
        if decision == "resample":
            if batch["state"] == "conditional": raise ApiError(409, "有条件放行后不能直接改为再取样")
            new_state = "awaiting_resample"
        elif decision == "reject":
            new_state = "rejected"
        elif any(d["severity"] == "critical" for d in open_deviations):
            raise ApiError(409, "未关闭的关键偏差阻止放行")
        elif decision == "release" and open_deviations:
            raise ApiError(409, "仍有未关闭偏差，不能正式放行")
        elif decision == "conditional":
            for deviation in open_deviations:
                if not deviation["exception_reason"] or not after_now(deviation["exception_until"]):
                    raise ApiError(409, f"偏差 {deviation['id']} 没有有效例外批准")
            if not exception_code.strip(): raise ApiError(400, "有条件放行必须提供例外编号")
            new_state = "conditional"
        else:
            new_state = "released"
        with self.conn:
            cur = self.conn.execute("""INSERT INTO decisions(batch_id,revision,decision,rationale,exception_code,decided_by,created_at)
                                     VALUES(?,?,?,?,?,?,?)""", (batch_id, batch["revision"], decision, rationale, exception_code or None, actor, now()))
            updated = self.conn.execute("UPDATE batches SET state=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                                        (new_state, now(), batch_id, expected_revision))
            if updated.rowcount != 1: raise ApiError(409, "并发放行冲突")
            if new_state in {"released", "conditional"}:
                self._create_summary(batch_id, "release", "放行质量摘要", actor)
            self.store.audit(actor, "batch.decision", "batch", batch_id, {"decision": decision, "revision": batch["revision"], "state": new_state, "exception_code": exception_code})
        return {"decision": dict(self._row("decisions", cur.lastrowid)), "batch": self.batch_detail(batch_id)["batch"]}

    def submit_post_release(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, data_type: str, payload: dict, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"operator", "inspector", "lab"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        if batch["state"] not in {"released", "conditional", "recall_review"}: raise ApiError(409, "仅已放行批次可登记放行后补录数据")
        severity, passed = self._validate_post_release(data_type, payload)
        with self.conn:
            cur = self.conn.execute("""INSERT INTO post_release_records(batch_id,data_type,payload_json,severity,passed,status,created_by,created_at)
                                     VALUES(?,?,?,?,?,'pending',?,?)""",
                                    (batch_id, data_type, j(payload), severity, passed, actor, now()))
            self._advance_released_batch(batch_id, expected_revision, batch["state"])
            self.store.audit(actor, "post_release.submit", "post_release", cur.lastrowid, {"batch_id": batch_id, "data_type": data_type, "severity": severity, "passed": passed})
        return self._post_release_dict(self._row("post_release_records", cur.lastrowid))

    @staticmethod
    def _validate_post_release(data_type: str, payload: dict) -> tuple[str | None, int | None]:
        if data_type == "deviation":
            severity = str(payload.get("severity", ""))
            if severity not in {"critical", "minor"} or not str(payload.get("title", "")).strip(): raise ApiError(400, "补录偏差等级或描述不合法")
            return severity, None
        if data_type == "test":
            spec_min, spec_max, result = float(payload.get("spec_min", 0)), float(payload.get("spec_max", 0)), float(payload.get("result", 0))
            if not str(payload.get("test_type", "")).strip() or spec_min > spec_max: raise ApiError(400, "补录检验项目或标准不合法")
            return None, int(spec_min <= result <= spec_max)
        if data_type == "stability":
            spec_limit, result = float(payload.get("spec_limit", 0)), float(payload.get("result", 0))
            if not str(payload.get("condition", "")).strip() or not str(payload.get("timepoint", "")).strip(): raise ApiError(400, "补录稳定性条件或时间点不合法")
            return None, int(result <= spec_limit)
        raise ApiError(400, "补录数据类型不合法")

    def assess_post_release(self, actor: str | None, role: str | None, record_id: int, outcome: str, note: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"qa"})
        record = self._row("post_release_records", record_id); batch = self._row("batches", record["batch_id"])
        if record["status"] != "pending": raise ApiError(409, "该补录记录已完成影响评估")
        if outcome not in {"pass", "fail"}: raise ApiError(400, "影响评估结论不合法")
        if not note.strip(): raise ApiError(400, "必须填写影响评估说明")
        if outcome == "pass" and record["passed"] == 0: raise ApiError(409, "不合格数据不能作为补充证据，必须评估为不合格")
        if outcome == "pass" and record["severity"] == "critical": raise ApiError(409, "关键偏差必须进入召回待审")
        stamp = now()
        with self.conn:
            if outcome == "pass":
                self.conn.execute("UPDATE post_release_records SET status='accepted',assessment=?,assessed_by=?,assessed_at=? WHERE id=? AND status='pending'",
                                  (note, actor, stamp, record_id))
                self._advance_released_batch(batch["id"], expected_revision, batch["state"])
                self._create_summary(batch["id"], "supplement", f"放行后补充证据（记录 {record_id}）：{note}", actor)
            else:
                self.conn.execute("UPDATE post_release_records SET status='recall_review',assessment=?,assessed_by=?,assessed_at=? WHERE id=? AND status='pending'",
                                  (note, actor, stamp, record_id))
                self._advance_released_batch(batch["id"], expected_revision, "recall_review")
            self.store.audit(actor, "post_release.assess", "post_release", record_id, {"batch_id": batch["id"], "outcome": outcome, "note": note})
        return {"record": self._post_release_dict(self._row("post_release_records", record_id)), "batch": self.batch_detail(batch["id"])["batch"]}

    def review_recall(self, actor: str | None, role: str | None, batch_id: int, outcome: str, conclusion: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"qa"})
        batch = self._row("batches", batch_id)
        if batch["state"] != "recall_review": raise ApiError(409, "批次不在召回待审状态")
        if outcome not in {"recall", "clear"}: raise ApiError(400, "复核结论不合法")
        if not conclusion.strip(): raise ApiError(400, "必须填写复核结论")
        new_state, stamp = ("recalled" if outcome == "recall" else "released"), now()
        with self.conn:
            self.conn.execute("""UPDATE post_release_records SET status='closed',conclusion=?,reviewed_by=?,reviewed_at=?
                               WHERE batch_id=? AND status='recall_review'""", (conclusion, actor, stamp, batch_id))
            self._advance_released_batch(batch_id, expected_revision, new_state)
            self._create_summary(batch_id, "recall_review", f"召回待审复核：{conclusion}", actor)
            self.store.audit(actor, "batch.recall_review", "batch", batch_id, {"outcome": outcome, "state": new_state, "conclusion": conclusion})
        return {"batch": self.batch_detail(batch_id)["batch"], "state": new_state}

    def _advance_released_batch(self, batch_id: int, expected_revision: int, next_state: str) -> None:
        batch = self._row("batches", batch_id)
        if batch["state"] not in {"released", "conditional", "recall_review"}: raise ApiError(409, "批次状态不允许放行后操作")
        if int(expected_revision) != int(batch["revision"]): raise ApiError(409, "批次版本冲突")
        cur = self.conn.execute("UPDATE batches SET state=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                                (next_state, now(), batch_id, expected_revision))
        if cur.rowcount != 1: raise ApiError(409, "并发更新冲突")

    def _snapshot(self, batch_id: int) -> dict:
        def rows(name: str) -> list[dict]: return [dict(row) for row in self.conn.execute(f"SELECT * FROM {name} WHERE batch_id=? ORDER BY id", (batch_id,))]
        return {"batch": self._batch_dict(self._row("batches", batch_id)), "deviations": rows("deviations"), "tests": rows("tests"),
                "rework": rows("rework"), "supplier_changes": rows("supplier_changes"), "stability": rows("stability"),
                "decisions": rows("decisions"),
                "supplementary_evidence": [self._post_release_dict(row) for row in self.conn.execute(
                    "SELECT * FROM post_release_records WHERE batch_id=? AND status IN ('accepted','closed') ORDER BY id", (batch_id,))]}

    def _create_summary(self, batch_id: int, source: str, note: str, actor: str) -> int:
        version = self.conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM quality_summaries WHERE batch_id=?", (batch_id,)).fetchone()[0]
        self.conn.execute("INSERT INTO quality_summaries(batch_id,version,source,snapshot_json,note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                          (batch_id, version, source, j(self._snapshot(batch_id)), note, actor, now()))
        return version

    def summaries(self, batch_id: int) -> list[dict]:
        self._row("batches", batch_id)
        return [self._summary_dict(row) for row in self.conn.execute("SELECT * FROM quality_summaries WHERE batch_id=? ORDER BY version", (batch_id,))]

    def pending_post_release(self) -> list[dict]:
        return [self._post_release_dict(row) for row in self.conn.execute(
            "SELECT * FROM post_release_records WHERE status IN ('pending','recall_review') ORDER BY id")]

    @staticmethod
    def _summary_dict(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "batch_id": row["batch_id"], "version": row["version"], "source": row["source"],
                "note": row["note"], "snapshot": json.loads(row["snapshot_json"]), "created_by": row["created_by"], "created_at": row["created_at"]}

    @staticmethod
    def _post_release_dict(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "batch_id": row["batch_id"], "data_type": row["data_type"], "payload": json.loads(row["payload_json"]),
                "severity": row["severity"], "passed": None if row["passed"] is None else bool(row["passed"]), "status": row["status"],
                "assessment": row["assessment"], "assessed_by": row["assessed_by"], "assessed_at": row["assessed_at"],
                "conclusion": row["conclusion"], "reviewed_by": row["reviewed_by"], "reviewed_at": row["reviewed_at"],
                "created_by": row["created_by"], "created_at": row["created_at"]}

    def _advance_batch(self, batch_id: int, expected_revision: int, next_state: str) -> None:
        batch = self._row("batches", batch_id)
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "终态批次不可修改")
        if int(expected_revision) != int(batch["revision"]): raise ApiError(409, "批次版本冲突")
        cur = self.conn.execute("UPDATE batches SET state=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                                (next_state, now(), batch_id, expected_revision))
        if cur.rowcount != 1: raise ApiError(409, "并发更新冲突")

    def batch_detail(self, batch_id: int) -> dict:
        batch = self._batch_dict(self._row("batches", batch_id))
        def rows(name: str) -> list[dict]: return [dict(row) for row in self.conn.execute(f"SELECT * FROM {name} WHERE batch_id=? ORDER BY id", (batch_id,))]
        return {"batch": batch, "deviations": rows("deviations"), "tests": rows("tests"), "rework": rows("rework"),
                "supplier_changes": rows("supplier_changes"), "stability": rows("stability"),
                "decisions": rows("decisions"), "summaries": self.summaries(batch_id),
                "post_release": [self._post_release_dict(row) for row in self.conn.execute(
                    "SELECT * FROM post_release_records WHERE batch_id=? ORDER BY id", (batch_id,))]}

    def _batch_dict(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "factory_id": row["factory_id"], "batch_no": row["batch_no"], "product": row["product"],
                "mfg_date": row["mfg_date"], "expiry_date": row["expiry_date"], "state": row["state"], "revision": row["revision"]}

    @staticmethod
    def _deviation_dict(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "batch_id": row["batch_id"], "severity": row["severity"], "title": row["title"], "due_at": row["due_at"],
                "status": row["status"], "corrective_action": row["corrective_action"], "exception_reason": row["exception_reason"],
                "exception_until": row["exception_until"], "exception_approved_by": row["exception_approved_by"]}

    @staticmethod
    def _test_dict(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "batch_id": row["batch_id"], "test_type": row["test_type"], "result": row["result"],
                "spec_min": row["spec_min"], "spec_max": row["spec_max"], "passed": bool(row["passed"]), "round": row["round"]}

    def state(self) -> dict:
        return {"factories": [dict(row) for row in self.conn.execute("SELECT * FROM factories ORDER BY id")],
                "batches": [self._batch_dict(row) for row in self.conn.execute("SELECT * FROM batches ORDER BY id DESC")],
                "post_release_pending": self.pending_post_release(),
                "audits": [dict(row) for row in self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 30")]}

    def seed(self) -> None:
        if not self.conn.execute("SELECT id FROM factories LIMIT 1").fetchone():
            self.register_factory("qa-demo", "qa", "F-DEMO", "演示工厂", "CN")


class Handler(BaseHTTPRequestHandler):
    service: BatchService

    def log_message(self, fmt: str, *args: object) -> None: sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))
    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body, ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        try: return json.loads(self.rfile.read(size)) if size else {}
        except json.JSONDecodeError as exc: raise ApiError(400, "JSON 请求体无效") from exc
    def _parts(self) -> list[str]: return [p for p in urlparse(self.path).path.strip("/").split("/") if p]

    def do_GET(self) -> None:
        try:
            p = self._parts()
            if p in (["health"], ["api", "health"]): out = {"status": "ok"}
            elif p == ["api", "state"]: out = self.service.state()
            elif p == ["api", "post-release", "pending"]: out = {"pending": self.service.pending_post_release()}
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "summaries": out = {"summaries": self.service.summaries(int(p[2]))}
            elif len(p) == 3 and p[:2] == ["api", "batches"]: out = self.service.batch_detail(int(p[2]))
            elif not p:
                page = (Path(__file__).parent / "static" / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page); return
            else: raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc: self._send(exc.status, {"error": exc.message})
        except Exception as exc: self._send(500, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            p, b = self._parts(), self._body(); actor, role = self.headers.get("X-Actor"), self.headers.get("X-Role")
            if p == ["api", "factories"]: out = self.service.register_factory(actor, role, b.get("code", ""), b.get("name", ""), b.get("country", ""))
            elif p == ["api", "batches"]: out = self.service.create_batch(actor, role, int(b.get("factory_id", 0)), b.get("batch_no", ""), b.get("product", ""), b.get("mfg_date", ""), b.get("expiry_date", ""))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "deviations": out = self.service.add_deviation(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("severity", ""), b.get("title", ""), b.get("due_at"), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "deviations"] and p[3] == "close": out = self.service.close_deviation(actor, role, int(p[2]), b.get("corrective_action", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "deviations"] and p[3] == "exception": out = self.service.approve_exception(actor, role, int(p[2]), b.get("reason", ""), b.get("until", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "tests": out = self.service.record_test(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("test_type", ""), float(b.get("result", 0)), float(b.get("spec_min", 0)), float(b.get("spec_max", 0)), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "rework": out = self.service.plan_rework(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("description", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "rework"] and p[3] == "complete": out = self.service.complete_rework(actor, role, int(b.get("factory_id", 0)), int(p[2]), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "supplier-changes": out = self.service.record_supplier_change(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("supplier", ""), b.get("change_type", ""), b.get("description", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "stability": out = self.service.record_stability(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("condition", ""), b.get("timepoint", ""), float(b.get("result", 0)), float(b.get("spec_limit", 0)), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "decide": out = self.service.decide(actor, role, int(p[2]), b.get("decision", ""), b.get("rationale", ""), int(b.get("expected_revision", -1)), b.get("exception_code", ""))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "post-release": out = self.service.submit_post_release(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("data_type", ""), b.get("payload") or {}, int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "post-release"] and p[3] == "assess": out = self.service.assess_post_release(actor, role, int(p[2]), b.get("outcome", ""), b.get("note", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "recall-review": out = self.service.review_recall(actor, role, int(p[2]), b.get("outcome", ""), b.get("conclusion", ""), int(b.get("expected_revision", -1)))
            else: raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc: self._send(exc.status, {"error": exc.message})
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc: self._send(400, {"error": str(exc)})
        except Exception as exc: self._send(500, {"error": str(exc)})


def run(port: int, db_path: str, seed: bool) -> None:
    store = Store(db_path); service = BatchService(store)
    if seed: service.seed()
    Handler.service = service
    print(f"batch release listening on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int, default=8214); parser.add_argument("--db", default=str(DB_PATH)); parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    if args.init: Store(args.db).close()
    if args.seed or not args.init: run(args.port, args.db, args.seed)


if __name__ == "__main__": main()
