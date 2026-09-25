"""接驳计划存储：站点需求与接驳班次的落库、版本复制和发布快照。

规则计算在 shuttle_rules，页面在 static/shuttle.html；本模块只负责
校验、持久化，以及把评估结果挂到版本生命周期（复制、发布）上。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from errors import DomainError
from shuttle_rules import CONFIRMED, PENDING, evaluate_plan

EDIT_ROLES = {"planner", "editor", "admin"}
MAX_SERVICE_MINUTE = 2880


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ShuttleStore:
    """挂在 Database 上的接驳计划存储，通过钩子跟随版本复制与发布。"""

    def __init__(self, db: Any):
        self.db = db
        self._init_schema()
        db.register_version_copy_hook(self.copy_version)
        db.register_snapshot_hook(self.snapshot_for)

    def _init_schema(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS shuttle_demands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    stop_id INTEGER NOT NULL REFERENCES stops(id),
                    needed INTEGER NOT NULL CHECK(needed > 0),
                    start_minute INTEGER,
                    end_minute INTEGER,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shuttle_trips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    crew TEXT NOT NULL,
                    start_minute INTEGER NOT NULL CHECK(start_minute >= 0),
                    end_minute INTEGER NOT NULL,
                    capacity INTEGER NOT NULL CHECK(capacity > 0),
                    stop_ids TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    CHECK(end_minute > start_minute)
                );
                """
            )

    # ---- 校验 ----

    def _require_edit_role(self, role: str) -> None:
        if role not in EDIT_ROLES:
            raise DomainError("没有修改接驳计划的权限", 403)

    def _require_draft(self, conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
        version = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        if not version:
            raise DomainError("方案版本不存在", 404)
        if version["status"] != "draft":
            raise DomainError("只有草稿版本可以修改接驳计划", 409)
        return version

    @staticmethod
    def _parse_window(payload: dict[str, Any], required: bool) -> tuple[int | None, int | None]:
        start, end = payload.get("start_minute"), payload.get("end_minute")
        if start is None and end is None:
            if required:
                raise DomainError("班次需要服务时段")
            return None, None
        if start is None or end is None:
            raise DomainError("开始和结束时间必须同时提供")
        try:
            start, end = int(start), int(end)
        except (TypeError, ValueError):
            raise DomainError("服务时段必须是分钟数")
        if start < 0 or end <= start or end > MAX_SERVICE_MINUTE:
            raise DomainError("服务时段范围不合法")
        return start, end

    def _validate_demand(self, conn: sqlite3.Connection, payload: dict[str, Any]) -> tuple[int, int, int | None, int | None, str]:
        try:
            stop_id = int(payload.get("stop_id"))
        except (TypeError, ValueError):
            raise DomainError("需求需要有效的站点")
        if not conn.execute("SELECT 1 FROM stops WHERE id=?", (stop_id,)).fetchone():
            raise DomainError("站点不存在", 404)
        try:
            needed = int(payload.get("needed"))
        except (TypeError, ValueError):
            raise DomainError("需求人数不合法")
        if needed < 1:
            raise DomainError("需求人数必须为正数")
        start, end = self._parse_window(payload, required=False)
        return stop_id, needed, start, end, str(payload.get("note", "") or "").strip()

    def _validate_trip(self, conn: sqlite3.Connection, payload: dict[str, Any]) -> tuple[str, str, int, int, int, list[int]]:
        label = str(payload.get("label", "")).strip()
        crew = str(payload.get("crew", "")).strip()
        if not label or not crew:
            raise DomainError("班次号和车组不能为空")
        start, end = self._parse_window(payload, required=True)
        assert start is not None and end is not None
        try:
            capacity = int(payload.get("capacity"))
        except (TypeError, ValueError):
            raise DomainError("载客量不合法")
        if capacity < 1:
            raise DomainError("载客量必须为正数")
        raw_ids = payload.get("stop_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise DomainError("班次至少停靠一个站点")
        stop_ids: list[int] = []
        for raw in raw_ids:
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                raise DomainError("停靠站点不合法")
            if sid in stop_ids:
                continue
            if not conn.execute("SELECT 1 FROM stops WHERE id=?", (sid,)).fetchone():
                raise DomainError("站点不存在", 404)
            stop_ids.append(sid)
        return label, crew, start, end, capacity, stop_ids

    # ---- 站点需求 ----

    def add_demand(self, version_id: int, payload: dict[str, Any], actor: str, role: str) -> dict[str, Any]:
        self._require_edit_role(role)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_draft(conn, version_id)
            stop_id, needed, start, end, note = self._validate_demand(conn, payload)
            cur = conn.execute(
                "INSERT INTO shuttle_demands(version_id,stop_id,needed,start_minute,end_minute,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (version_id, stop_id, needed, start, end, note, utcnow()))
            self.db._audit(conn, actor, "shuttle.demand.added", "version", version_id,
                           {"demand_id": cur.lastrowid, "stop_id": stop_id, "needed": needed})
        return self.get_plan(version_id)

    def update_demand(self, demand_id: int, payload: dict[str, Any], actor: str, role: str) -> dict[str, Any]:
        self._require_edit_role(role)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM shuttle_demands WHERE id=?", (demand_id,)).fetchone()
            if not row:
                raise DomainError("需求不存在", 404)
            self._require_draft(conn, row["version_id"])
            merged = {"stop_id": row["stop_id"], "needed": row["needed"],
                      "start_minute": row["start_minute"], "end_minute": row["end_minute"], "note": row["note"]}
            for key in merged:
                if key in payload:
                    merged[key] = payload[key]
            stop_id, needed, start, end, note = self._validate_demand(conn, merged)
            conn.execute("UPDATE shuttle_demands SET stop_id=?,needed=?,start_minute=?,end_minute=?,note=? WHERE id=?",
                         (stop_id, needed, start, end, note, demand_id))
            self.db._audit(conn, actor, "shuttle.demand.updated", "version", row["version_id"], {"demand_id": demand_id})
            version_id = int(row["version_id"])
        return self.get_plan(version_id)

    def delete_demand(self, demand_id: int, actor: str, role: str) -> dict[str, Any]:
        return self._delete("shuttle_demands", demand_id, actor, role, "需求不存在", "shuttle.demand.deleted")

    # ---- 接驳班次 ----

    def add_trip(self, version_id: int, payload: dict[str, Any], actor: str, role: str) -> dict[str, Any]:
        self._require_edit_role(role)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_draft(conn, version_id)
            label, crew, start, end, capacity, stop_ids = self._validate_trip(conn, payload)
            cur = conn.execute(
                "INSERT INTO shuttle_trips(version_id,label,crew,start_minute,end_minute,capacity,stop_ids,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (version_id, label, crew, start, end, capacity, json.dumps(stop_ids, ensure_ascii=False), utcnow()))
            self.db._audit(conn, actor, "shuttle.trip.added", "version", version_id,
                           {"trip_id": cur.lastrowid, "label": label, "crew": crew})
        return self.get_plan(version_id)

    def update_trip(self, trip_id: int, payload: dict[str, Any], actor: str, role: str) -> dict[str, Any]:
        self._require_edit_role(role)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM shuttle_trips WHERE id=?", (trip_id,)).fetchone()
            if not row:
                raise DomainError("班次不存在", 404)
            self._require_draft(conn, row["version_id"])
            merged = {"label": row["label"], "crew": row["crew"], "start_minute": row["start_minute"],
                      "end_minute": row["end_minute"], "capacity": row["capacity"],
                      "stop_ids": json.loads(row["stop_ids"] or "[]")}
            for key in merged:
                if key in payload:
                    merged[key] = payload[key]
            label, crew, start, end, capacity, stop_ids = self._validate_trip(conn, merged)
            conn.execute("UPDATE shuttle_trips SET label=?,crew=?,start_minute=?,end_minute=?,capacity=?,stop_ids=? WHERE id=?",
                         (label, crew, start, end, capacity, json.dumps(stop_ids, ensure_ascii=False), trip_id))
            self.db._audit(conn, actor, "shuttle.trip.updated", "version", row["version_id"], {"trip_id": trip_id})
            version_id = int(row["version_id"])
        return self.get_plan(version_id)

    def delete_trip(self, trip_id: int, actor: str, role: str) -> dict[str, Any]:
        return self._delete("shuttle_trips", trip_id, actor, role, "班次不存在", "shuttle.trip.deleted")

    def _delete(self, table: str, row_id: int, actor: str, role: str, missing: str, action: str) -> dict[str, Any]:
        self._require_edit_role(role)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
            if not row:
                raise DomainError(missing, 404)
            self._require_draft(conn, row["version_id"])
            conn.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
            self.db._audit(conn, actor, action, "version", row["version_id"], {"id": row_id})
            version_id = int(row["version_id"])
        return self.get_plan(version_id)

    # ---- 查询与快照 ----

    def _collect(self, conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
        demands = [dict(r) for r in conn.execute(
            "SELECT * FROM shuttle_demands WHERE version_id=? ORDER BY id", (version_id,))]
        trips = []
        for r in conn.execute("SELECT * FROM shuttle_trips WHERE version_id=? ORDER BY id", (version_id,)):
            trip = dict(r)
            trip["stop_ids"] = [int(s) for s in json.loads(trip["stop_ids"] or "[]")]
            trips.append(trip)
        # 停运/跳站变更形成需要接驳覆盖的站点缺口。
        affected_map: dict[int, dict[str, Any]] = {}
        for r in conn.execute(
                "SELECT kind,stop_id,effective_start_minute,effective_end_minute FROM changes "
                "WHERE version_id=? AND kind IN ('stop_closure','skip_stop') AND stop_id IS NOT NULL ORDER BY id",
                (version_id,)):
            sid = int(r["stop_id"])
            entry = affected_map.setdefault(sid, {"stop_id": sid, "kinds": set(), "start_minute": None, "end_minute": None})
            entry["kinds"].add(r["kind"])
            if r["effective_start_minute"] is None:
                entry["start_minute"] = entry["end_minute"] = None
                entry["all_day"] = True
            elif not entry.get("all_day"):
                entry["start_minute"] = r["effective_start_minute"] if entry["start_minute"] is None else min(entry["start_minute"], r["effective_start_minute"])
                entry["end_minute"] = r["effective_end_minute"] if entry["end_minute"] is None else max(entry["end_minute"], r["effective_end_minute"])
        affected = [{"stop_id": e["stop_id"],
                     "kind": "stop_closure" if "stop_closure" in e["kinds"] else "skip_stop",
                     "start_minute": e["start_minute"], "end_minute": e["end_minute"]}
                    for e in affected_map.values()]
        stop_names = {int(r["id"]): {"code": r["code"], "name": r["name"]}
                      for r in conn.execute("SELECT id,code,name FROM stops")}
        return {"demands": demands, "trips": trips, "affected": affected, "stop_names": stop_names}

    def _assemble(self, conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
        ctx = self._collect(conn, version_id)
        report = evaluate_plan(ctx["demands"], ctx["trips"], [s["stop_id"] for s in ctx["affected"]])
        names = ctx["stop_names"]

        def info(stop_id: int) -> dict[str, Any]:
            return names.get(int(stop_id), {})

        trips = []
        for trip in ctx["trips"]:
            ev = report["trips"][trip["id"]]
            trips.append({**trip,
                          "stop_names": [info(s).get("name", str(s)) for s in trip["stop_ids"]],
                          "status": ev["status"], "issues": ev["issues"]})
        demands = []
        for demand in ctx["demands"]:
            ev = report["demands"][demand["id"]]
            demands.append({**demand,
                            "stop_code": info(demand["stop_id"]).get("code"),
                            "stop_name": info(demand["stop_id"]).get("name"),
                            **ev})
        stations = [{**s, "code": info(s["stop_id"]).get("code"), "name": info(s["stop_id"]).get("name")}
                    for s in report["stations"]]
        gaps = [{**g, "stop_code": info(g["stop_id"]).get("code"), "stop_name": info(g["stop_id"]).get("name")}
                for g in report["gaps"]]
        affected = [{**s, "code": info(s["stop_id"]).get("code"), "name": info(s["stop_id"]).get("name")}
                    for s in ctx["affected"]]
        return {"affected_stations": affected, "demands": demands, "trips": trips,
                "stations": stations, "gaps": gaps, "summary": report["summary"]}

    def get_plan(self, version_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            version = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise DomainError("方案版本不存在", 404)
            plan = self._assemble(conn, version_id)
        plan.update({"version_id": version["id"], "version_no": version["version_no"],
                     "version_status": version["status"], "disruption_id": version["disruption_id"]})
        return plan

    # ---- 版本生命周期钩子 ----

    def copy_version(self, conn: sqlite3.Connection, parent_id: int, new_id: int) -> None:
        """版本复制时把需求与班次一起复制到新草稿。"""
        for r in conn.execute("SELECT * FROM shuttle_demands WHERE version_id=?", (parent_id,)):
            conn.execute(
                "INSERT INTO shuttle_demands(version_id,stop_id,needed,start_minute,end_minute,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (new_id, r["stop_id"], r["needed"], r["start_minute"], r["end_minute"], r["note"], utcnow()))
        for r in conn.execute("SELECT * FROM shuttle_trips WHERE version_id=?", (parent_id,)):
            conn.execute(
                "INSERT INTO shuttle_trips(version_id,label,crew,start_minute,end_minute,capacity,stop_ids,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (new_id, r["label"], r["crew"], r["start_minute"], r["end_minute"], r["capacity"], r["stop_ids"], utcnow()))

    def snapshot_for(self, conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
        """发布时把已确认班次写进快照，待确认班次与缺口一并记录。"""
        plan = self._assemble(conn, version_id)
        confirmed = [t for t in plan["trips"] if t["status"] == CONFIRMED]
        pending = [t for t in plan["trips"] if t["status"] == PENDING]
        return {"shuttle": {"confirmed_trips": confirmed, "pending_trips": pending,
                            "demands": plan["demands"], "stations": plan["stations"],
                            "gaps": plan["gaps"], "summary": plan["summary"]}}
