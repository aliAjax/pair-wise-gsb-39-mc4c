"""接驳计划台的持久化层：需求与班次落库、状态重算、版本复制和发布快照。

挂在 Database 上复用同一连接与审计，表结构随主库一起初始化；
校验和冲突判断全部交给 shuttle_rules。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from shuttle_rules import (
    RuleViolation,
    build_coverage,
    clock_label,
    evaluate_shifts,
    normalize_demand,
    normalize_shift,
)

EDIT_ROLES = {"planner", "editor", "admin"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ShuttleStore:
    def __init__(self, db: Any):
        self.db = db

    def init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS shuttle_demands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                stop_id INTEGER NOT NULL REFERENCES stops(id),
                headcount INTEGER NOT NULL CHECK(headcount >= 1),
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(version_id,stop_id)
            );
            CREATE TABLE IF NOT EXISTS shuttle_shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                crew TEXT NOT NULL,
                stop_id INTEGER NOT NULL REFERENCES stops(id),
                start_minute INTEGER NOT NULL CHECK(start_minute >= 0),
                end_minute INTEGER NOT NULL CHECK(end_minute > start_minute AND end_minute <= 2880),
                capacity INTEGER NOT NULL CHECK(capacity >= 1),
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed')),
                note TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

    def _draft_version(self, conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
        row = self._version_or_404(conn, version_id)
        if row["status"] != "draft":
            raise RuleViolation("只有草稿版本可以修改接驳计划", 409)
        return row

    def _version_or_404(self, conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise RuleViolation("方案版本不存在", 404)
        return row

    def _require_stop(self, conn: sqlite3.Connection, stop_id: int) -> None:
        if not conn.execute("SELECT 1 FROM stops WHERE id=?", (stop_id,)).fetchone():
            raise RuleViolation("站点不存在", 404)

    def _load(self, conn: sqlite3.Connection, version_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        demands = [dict(r) for r in conn.execute("SELECT * FROM shuttle_demands WHERE version_id=? ORDER BY id", (version_id,)).fetchall()]
        shifts = [dict(r) for r in conn.execute("SELECT * FROM shuttle_shifts WHERE version_id=? ORDER BY id", (version_id,)).fetchall()]
        return demands, shifts

    def _closed_stop_ids(self, conn: sqlite3.Connection, version_id: int) -> set[int]:
        rows = conn.execute(
            "SELECT DISTINCT stop_id FROM changes WHERE version_id=? AND kind='stop_closure' AND stop_id IS NOT NULL",
            (version_id,),
        ).fetchall()
        return {int(r["stop_id"]) for r in rows}

    def _recompute(self, conn: sqlite3.Connection, version_id: int) -> None:
        """每次需求或班次变动后重算整个版本的班次状态，保证结果确定。"""
        demands, shifts = self._load(conn, version_id)
        for shift_id, evaluation in evaluate_shifts(demands, shifts).items():
            conn.execute("UPDATE shuttle_shifts SET status=?,note=? WHERE id=?",
                         (evaluation["status"], evaluation["note"], shift_id))

    def add_demand(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        if role not in EDIT_ROLES:
            raise RuleViolation("没有登记接驳需求的权限", 403)
        demand = normalize_demand(payload)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._draft_version(conn, version_id)
            self._require_stop(conn, demand["stop_id"])
            now = _utcnow()
            conn.execute(
                """INSERT INTO shuttle_demands(version_id,stop_id,headcount,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(version_id,stop_id) DO UPDATE SET headcount=excluded.headcount,updated_at=excluded.updated_at""",
                (version_id, demand["stop_id"], demand["headcount"], actor, now, now),
            )
            self._recompute(conn, version_id)
            self.db._audit(conn, actor, "shuttle.demand.saved", "version", version_id, demand)
        return self.coverage(version_id)

    def add_shift(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        if role not in EDIT_ROLES:
            raise RuleViolation("没有安排接驳班次的权限", 403)
        shift = normalize_shift(payload)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._draft_version(conn, version_id)
            self._require_stop(conn, shift["stop_id"])
            cur = conn.execute(
                """INSERT INTO shuttle_shifts(version_id,crew,stop_id,start_minute,end_minute,capacity,status,note,created_by,created_at)
                   VALUES(?,?,?,?,?,?,'pending','',?,?)""",
                (version_id, shift["crew"], shift["stop_id"], shift["start_minute"], shift["end_minute"],
                 shift["capacity"], actor, _utcnow()),
            )
            self._recompute(conn, version_id)
            self.db._audit(conn, actor, "shuttle.shift.saved", "version", version_id, {"shift_id": int(cur.lastrowid), **shift})
        return self.coverage(version_id)

    def coverage(self, version_id: int) -> dict[str, Any]:
        """按版本查看覆盖情况；已发布版本的行不会被草稿改动影响，可随时回看。"""
        with self.db.connect() as conn:
            version = self._version_or_404(conn, version_id)
            demands, shifts = self._load(conn, version_id)
            closed = self._closed_stop_ids(conn, version_id)
            stops = {int(r["id"]): dict(r) for r in conn.execute("SELECT * FROM stops").fetchall()}
        report = build_coverage(demands, shifts, closed, stops)
        for row in report["shifts"]:
            stop = stops.get(int(row["stop_id"]), {})
            row["stop_code"] = stop.get("code", str(row["stop_id"]))
            row["stop_name"] = stop.get("name", "")
            row["start_clock"] = clock_label(row["start_minute"])
            row["end_clock"] = clock_label(row["end_minute"])
        report["closed_stops"] = [
            {"stop_id": sid, "stop_code": stops.get(sid, {}).get("code", str(sid)), "stop_name": stops.get(sid, {}).get("name", "")}
            for sid in sorted(closed)
        ]
        report.update({
            "version_id": version_id,
            "version_no": version["version_no"],
            "version_status": version["status"],
            "disruption_id": version["disruption_id"],
        })
        return report

    def copy_to_version(self, conn: sqlite3.Connection, source_id: int, new_id: int) -> None:
        """创建新版本时把需求和班次一起复制过去，再按新行重算状态。"""
        demands, shifts = self._load(conn, source_id)
        now = _utcnow()
        for d in demands:
            conn.execute(
                "INSERT INTO shuttle_demands(version_id,stop_id,headcount,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (new_id, d["stop_id"], d["headcount"], d["created_by"], now, now),
            )
        for s in shifts:
            conn.execute(
                """INSERT INTO shuttle_shifts(version_id,crew,stop_id,start_minute,end_minute,capacity,status,note,created_by,created_at)
                   VALUES(?,?,?,?,?,?,'pending','',?,?)""",
                (new_id, s["crew"], s["stop_id"], s["start_minute"], s["end_minute"], s["capacity"], s["created_by"], now),
            )
        self._recompute(conn, new_id)

    def snapshot_payload(self, conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
        """发布时写入快照的接驳计划：只包含已确认班次，另记录待确认数量和缺口。"""
        demands, shifts = self._load(conn, version_id)
        closed = self._closed_stop_ids(conn, version_id)
        stops = {int(r["id"]): dict(r) for r in conn.execute("SELECT * FROM stops").fetchall()}
        report = build_coverage(demands, shifts, closed, stops)
        return {
            "demands": [
                {"stop_id": d["stop_id"], "stop_code": d["stop_code"], "headcount": d["headcount"]}
                for d in report["demands"]
            ],
            "confirmed_shifts": [
                {"id": s["id"], "crew": s["crew"], "stop_id": s["stop_id"],
                 "start_minute": s["start_minute"], "end_minute": s["end_minute"], "capacity": s["capacity"]}
                for s in report["shifts"] if s["status"] == "confirmed"
            ],
            "pending_shifts": report["pending_shifts"],
            "plan_status": report["plan_status"],
            "gaps": report["gaps"],
        }
