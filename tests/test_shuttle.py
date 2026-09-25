import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import Database, Handler, seed_demo
from errors import DomainError
from shuttle_rules import evaluate_plan, windows_overlap
from shuttle_store import ShuttleStore


class ShuttleRulesTest(unittest.TestCase):
    def test_windows_overlap_half_open(self):
        self.assertTrue(windows_overlap(100, 200, 150, 250))
        self.assertFalse(windows_overlap(100, 200, 200, 300))  # 首尾相接不算重叠
        self.assertFalse(windows_overlap(300, 400, 100, 200))

    def test_crew_overlap_marks_both_trips_pending(self):
        trips = [
            {"id": 1, "label": "A1", "crew": "车组A", "start_minute": 100, "end_minute": 200, "capacity": 50, "stop_ids": [1]},
            {"id": 2, "label": "A2", "crew": "车组A", "start_minute": 150, "end_minute": 260, "capacity": 50, "stop_ids": [1]},
            {"id": 3, "label": "B1", "crew": "车组B", "start_minute": 150, "end_minute": 260, "capacity": 50, "stop_ids": [1]},
        ]
        report = evaluate_plan([], trips)
        self.assertEqual(report["trips"][1]["status"], "pending")
        self.assertEqual(report["trips"][2]["status"], "pending")
        self.assertEqual(report["trips"][3]["status"], "confirmed")
        self.assertEqual(report["trips"][1]["issues"][0]["code"], "crew_overlap")
        self.assertIn("车组A时段重叠", report["trips"][1]["issues"][0]["message"])

    def test_pending_trips_do_not_count_toward_coverage(self):
        demands = [{"id": 1, "stop_id": 9, "needed": 80, "start_minute": None, "end_minute": None}]
        trips = [
            {"id": 1, "label": "A1", "crew": "车组A", "start_minute": 100, "end_minute": 200, "capacity": 60, "stop_ids": [9]},
            {"id": 2, "label": "A2", "crew": "车组A", "start_minute": 150, "end_minute": 250, "capacity": 60, "stop_ids": [9]},
        ]
        report = evaluate_plan(demands, trips)
        self.assertEqual(report["demands"][1]["covered_capacity"], 0)
        self.assertEqual(report["demands"][1]["gap"], 80)
        self.assertEqual(report["demands"][1]["issues"][0]["code"], "no_coverage")

    def test_capacity_shortage_writes_gap(self):
        demands = [{"id": 1, "stop_id": 9, "needed": 100, "start_minute": 100, "end_minute": 300}]
        trips = [{"id": 1, "label": "A1", "crew": "车组A", "start_minute": 100, "end_minute": 300, "capacity": 60, "stop_ids": [9]}]
        report = evaluate_plan(demands, trips)
        self.assertEqual(report["demands"][1]["status"], "pending")
        self.assertEqual(report["demands"][1]["gap"], 40)
        self.assertEqual(report["demands"][1]["issues"][0]["code"], "capacity_shortage")
        self.assertIn("缺口 40", report["demands"][1]["issues"][0]["message"])

    def test_demand_window_must_overlap_trip_window(self):
        demands = [{"id": 1, "stop_id": 9, "needed": 10, "start_minute": 500, "end_minute": 600}]
        trips = [{"id": 1, "label": "A1", "crew": "车组A", "start_minute": 100, "end_minute": 300, "capacity": 60, "stop_ids": [9]}]
        report = evaluate_plan(demands, trips)
        self.assertEqual(report["demands"][1]["issues"][0]["code"], "no_coverage")

    def test_station_rollup_and_summary(self):
        demands = [{"id": 1, "stop_id": 9, "needed": 40, "start_minute": None, "end_minute": None}]
        trips = [{"id": 1, "label": "A1", "crew": "车组A", "start_minute": 0, "end_minute": 600, "capacity": 40, "stop_ids": [9]}]
        report = evaluate_plan(demands, trips, affected_stop_ids=[9, 10])
        stations = {s["stop_id"]: s for s in report["stations"]}
        self.assertEqual(stations[9]["status"], "covered")
        self.assertEqual(stations[10]["status"], "no_demand")
        self.assertEqual(report["summary"]["total_needed"], 40)
        self.assertEqual(report["summary"]["total_covered"], 40)
        self.assertEqual(report["summary"]["stations_with_gap"], 1)


class ShuttleFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.db = Database(self.db_path)
        self.shuttle = ShuttleStore(self.db)
        seed_demo(self.db)
        self.stops = {row["code"]: int(row["id"]) for row in self.db.list_stops()}
        disruption = self.db.create_disruption("planner-01", {"code": "D-100", "name": "南门码头停运",
                                                              "starts_at": "2026-09-25T18:00:00+08:00",
                                                              "ends_at": "2026-09-26T02:00:00+08:00"}, "planner")
        self.version_id = disruption["draft_version_id"]
        self.disruption_id = disruption["id"]
        self.db.add_change(self.version_id, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S3"],
                                                           "effective_start_minute": 1080, "effective_end_minute": 1560}, "planner")
        self.db.add_change(self.version_id, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"],
                                                           "effective_start_minute": 1080, "effective_end_minute": 1560}, "planner")

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self):
        return self.shuttle.get_plan(self.version_id)

    def _approve_and_publish(self, version_id):
        self.db.transition(version_id, "planner-01", "planner", "submit")
        self.db.transition(version_id, "reviewer-01", "reviewer", "approve")
        return self.db.transition(version_id, "reviewer-01", "reviewer", "publish")

    def test_affected_stations_come_from_closures(self):
        plan = self._plan()
        self.assertEqual({s["stop_id"] for s in plan["affected_stations"]}, {self.stops["S3"], self.stops["S4"]})
        self.assertEqual({s["status"] for s in plan["stations"]}, {"no_demand"})
        self.assertEqual(len(plan["gaps"]), 2)
        self.assertTrue(all(g["reason"] == "no_demand" for g in plan["gaps"]))

    def test_overlap_stays_pending_until_fixed(self):
        self.shuttle.add_demand(self.version_id, {"stop_id": self.stops["S3"], "needed": 50}, "planner-01", "planner")
        self.shuttle.add_trip(self.version_id, {"label": "接驳-1", "crew": "车组A", "start_minute": 1080,
                                                "end_minute": 1300, "capacity": 60, "stop_ids": [self.stops["S3"]]}, "planner-01", "planner")
        plan = self.shuttle.add_trip(self.version_id, {"label": "接驳-2", "crew": "车组A", "start_minute": 1290,
                                                       "end_minute": 1500, "capacity": 60, "stop_ids": [self.stops["S3"]]}, "planner-01", "planner")
        self.assertEqual({t["label"]: t["status"] for t in plan["trips"]}, {"接驳-1": "pending", "接驳-2": "pending"})
        self.assertEqual(plan["demands"][0]["status"], "pending")  # 没有已确认班次可用
        second = next(t for t in plan["trips"] if t["label"] == "接驳-2")
        plan = self.shuttle.update_trip(second["id"], {"crew": "车组B"}, "planner-01", "planner")
        self.assertEqual({t["status"] for t in plan["trips"]}, {"confirmed"})
        self.assertEqual(plan["demands"][0]["status"], "confirmed")

    def test_capacity_gap_closed_by_second_trip(self):
        self.shuttle.add_demand(self.version_id, {"stop_id": self.stops["S3"], "needed": 100,
                                                  "start_minute": 1080, "end_minute": 1320}, "planner-01", "planner")
        plan = self.shuttle.add_trip(self.version_id, {"label": "接驳-1", "crew": "车组A", "start_minute": 1080,
                                                       "end_minute": 1560, "capacity": 60, "stop_ids": [self.stops["S3"]]}, "planner-01", "planner")
        self.assertEqual(plan["demands"][0]["gap"], 40)
        self.assertEqual(plan["demands"][0]["issues"][0]["code"], "capacity_shortage")
        plan = self.shuttle.add_trip(self.version_id, {"label": "接驳-2", "crew": "车组B", "start_minute": 1080,
                                                       "end_minute": 1560, "capacity": 40, "stop_ids": [self.stops["S3"]]}, "planner-01", "planner")
        self.assertEqual(plan["demands"][0]["gap"], 0)
        self.assertEqual(plan["demands"][0]["status"], "confirmed")
        station = next(s for s in plan["stations"] if s["stop_id"] == self.stops["S3"])
        self.assertEqual(station["status"], "covered")

    def test_uncovered_station_keeps_full_gap(self):
        plan = self.shuttle.add_demand(self.version_id, {"stop_id": self.stops["S4"], "needed": 80}, "planner-01", "planner")
        demand = plan["demands"][0]
        self.assertEqual(demand["status"], "pending")
        self.assertEqual(demand["gap"], 80)
        self.assertEqual(demand["issues"][0]["code"], "no_coverage")
        station = next(s for s in plan["stations"] if s["stop_id"] == self.stops["S4"])
        self.assertEqual(station["status"], "uncovered")

    def test_publish_snapshot_keeps_confirmed_only_and_draft_isolation(self):
        self.shuttle.add_demand(self.version_id, {"stop_id": self.stops["S3"], "needed": 100}, "planner-01", "planner")
        self.shuttle.add_trip(self.version_id, {"label": "接驳-1", "crew": "车组A", "start_minute": 1080,
                                                "end_minute": 1560, "capacity": 100, "stop_ids": [self.stops["S3"]]}, "planner-01", "planner")
        # 一对时段重叠的班次留在待确认
        self.shuttle.add_trip(self.version_id, {"label": "接驳-2", "crew": "车组B", "start_minute": 1100,
                                                "end_minute": 1200, "capacity": 30, "stop_ids": [self.stops["S4"]]}, "planner-01", "planner")
        self.shuttle.add_trip(self.version_id, {"label": "接驳-3", "crew": "车组B", "start_minute": 1150,
                                                "end_minute": 1300, "capacity": 30, "stop_ids": [self.stops["S4"]]}, "planner-01", "planner")
        published = self._approve_and_publish(self.version_id)
        snapshot = json.loads(published["snapshot"])
        shuttle = snapshot["shuttle"]
        self.assertEqual([t["label"] for t in shuttle["confirmed_trips"]], ["接驳-1"])
        self.assertEqual({t["label"] for t in shuttle["pending_trips"]}, {"接驳-2", "接驳-3"})
        self.assertTrue(any(g["reason"] == "no_demand" and g["stop_id"] == self.stops["S4"] for g in shuttle["gaps"]))
        self.assertEqual(shuttle["summary"]["total_gap"], 0)

        # 草稿再改只影响新版本：v2 复制后继续编辑，v1 计划和快照不变
        v2 = self.db.create_version_copy(self.disruption_id, self.version_id, "planner-02", "planner")["id"]
        plan_v2 = self.shuttle.get_plan(v2)
        self.assertEqual(len(plan_v2["trips"]), 3)
        self.assertEqual(len(plan_v2["demands"]), 1)
        self.shuttle.add_trip(v2, {"label": "接驳-4", "crew": "车组C", "start_minute": 1080,
                                   "end_minute": 1560, "capacity": 80, "stop_ids": [self.stops["S4"]]}, "planner-02", "planner")
        self.assertEqual(len(self.shuttle.get_plan(v2)["trips"]), 4)
        self.assertEqual(len(self._plan()["trips"]), 3)
        again = self.db.get_version(self.version_id)["snapshot"]
        self.assertEqual(len(again["shuttle"]["confirmed_trips"]), 1)

    def test_reopen_keeps_versions_and_coverage(self):
        self.shuttle.add_demand(self.version_id, {"stop_id": self.stops["S3"], "needed": 60}, "planner-01", "planner")
        self.shuttle.add_trip(self.version_id, {"label": "接驳-1", "crew": "车组A", "start_minute": 1080,
                                                "end_minute": 1560, "capacity": 60, "stop_ids": [self.stops["S3"]]}, "planner-01", "planner")
        self._approve_and_publish(self.version_id)
        # 模拟重开：同一数据库文件重新建库对象，仍可按版本查看覆盖
        reopened_db = Database(self.db_path)
        reopened_shuttle = ShuttleStore(reopened_db)
        plan = reopened_shuttle.get_plan(self.version_id)
        self.assertEqual(plan["version_status"], "published")
        self.assertEqual(plan["summary"]["total_gap"], 0)
        snapshot = reopened_db.get_version(self.version_id)["snapshot"]
        self.assertEqual(snapshot["shuttle"]["confirmed_trips"][0]["label"], "接驳-1")
        with self.assertRaises(DomainError):
            reopened_shuttle.add_trip(self.version_id, {"label": "接驳-9", "crew": "车组Z", "start_minute": 1,
                                                        "end_minute": 2, "capacity": 1, "stop_ids": [self.stops["S3"]]}, "planner-01", "planner")

    def test_permissions_and_validation(self):
        with self.assertRaises(DomainError):
            self.shuttle.add_demand(self.version_id, {"stop_id": self.stops["S3"], "needed": 10}, "someone", "viewer")
        with self.assertRaises(DomainError):
            self.shuttle.add_demand(self.version_id, {"stop_id": self.stops["S3"], "needed": 10, "start_minute": 100}, "planner-01", "planner")
        with self.assertRaises(DomainError):
            self.shuttle.add_demand(self.version_id, {"stop_id": 99999, "needed": 10}, "planner-01", "planner")
        with self.assertRaises(DomainError):
            self.shuttle.add_trip(self.version_id, {"label": "X", "crew": "车组A", "start_minute": 200,
                                                    "end_minute": 100, "capacity": 10, "stop_ids": [self.stops["S3"]]}, "planner-01", "planner")
        with self.assertRaises(DomainError):
            self.shuttle.add_trip(self.version_id, {"label": "X", "crew": "车组A", "start_minute": 100,
                                                    "end_minute": 200, "capacity": 0, "stop_ids": [self.stops["S3"]]}, "planner-01", "planner")
        self.db.transition(self.version_id, "planner-01", "planner", "submit")
        with self.assertRaises(DomainError):
            self.shuttle.add_demand(self.version_id, {"stop_id": self.stops["S3"], "needed": 10}, "planner-01", "planner")


class ShuttleHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "http.db")
        self.shuttle = ShuttleStore(self.db)
        seed_demo(self.db)
        Handler.db = self.db
        Handler.shuttle = self.shuttle
        self._orig_log = Handler.log_message
        Handler.log_message = lambda *args: None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        disruption = self.db.create_disruption("planner-01", {"code": "D-HTTP", "name": "HTTP 演练",
                                                              "starts_at": "2026-09-25T00:00:00+08:00",
                                                              "ends_at": "2026-09-26T00:00:00+08:00"}, "planner")
        self.version_id = disruption["draft_version_id"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        Handler.log_message = self._orig_log
        self.tmp.cleanup()

    def _req(self, method, path, body=None, role="planner"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        headers = {"Content-Type": "application/json", "X-User": "planner-01", "X-Role": role}
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def test_plan_roundtrip_over_http(self):
        stops = {row["code"]: int(row["id"]) for row in self.db.list_stops()}
        status, plan = self._req("POST", f"/api/versions/{self.version_id}/shuttle/demands",
                                 {"stop_id": stops["S1"], "needed": 30})
        self.assertEqual(status, 201)
        self.assertEqual(plan["demands"][0]["status"], "pending")
        status, plan = self._req("POST", f"/api/versions/{self.version_id}/shuttle/trips",
                                 {"label": "接驳-1", "crew": "车组A", "start_minute": 0, "end_minute": 600,
                                  "capacity": 30, "stop_ids": [stops["S1"]]})
        self.assertEqual(status, 201)
        self.assertEqual(plan["demands"][0]["status"], "confirmed")
        status, plan = self._req("GET", f"/api/versions/{self.version_id}/shuttle")
        self.assertEqual(status, 200)
        self.assertEqual(plan["summary"]["total_gap"], 0)
        demand_id = plan["demands"][0]["id"]
        status, plan = self._req("DELETE", f"/api/shuttle/demands/{demand_id}")
        self.assertEqual(status, 200)
        self.assertEqual(plan["demands"], [])
        status, body = self._req("POST", f"/api/versions/{self.version_id}/shuttle/demands",
                                 {"stop_id": stops["S1"], "needed": 5}, role="viewer")
        self.assertEqual(status, 403)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
