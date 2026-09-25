import json
import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo
from shuttle_rules import RuleViolation, evaluate_shifts, normalize_shift, windows_overlap


class ShuttleRulesTest(unittest.TestCase):
    def test_normalize_shift_window_and_capacity(self):
        with self.assertRaises(RuleViolation):
            normalize_shift({"crew": "车组A", "stop_id": 1, "start_minute": 1500, "end_minute": 1500, "capacity": 40})
        with self.assertRaises(RuleViolation):
            normalize_shift({"crew": "车组A", "stop_id": 1, "start_minute": 0, "end_minute": 2881, "capacity": 40})
        with self.assertRaises(RuleViolation):
            normalize_shift({"crew": "车组A", "stop_id": 1, "start_minute": 0, "end_minute": 60, "capacity": 0})
        with self.assertRaises(RuleViolation):
            normalize_shift({"crew": " ", "stop_id": 1, "start_minute": 0, "end_minute": 60, "capacity": 40})
        ok = normalize_shift({"crew": "车组A", "stop_id": 1, "start_minute": 1430, "end_minute": 1500, "capacity": 40})
        self.assertEqual(ok["end_minute"], 1500)

    def test_windows_overlap_touching_is_not_overlap(self):
        self.assertFalse(windows_overlap(1320, 1440, 1440, 1560))
        self.assertTrue(windows_overlap(1320, 1440, 1439, 1560))

    def test_evaluate_shifts_marks_both_sides_of_crew_overlap(self):
        shifts = [
            {"id": 1, "crew": "车组A", "stop_id": 4, "start_minute": 1320, "end_minute": 1440, "capacity": 50},
            {"id": 2, "crew": "车组A", "stop_id": 4, "start_minute": 1400, "end_minute": 1500, "capacity": 50},
            {"id": 3, "crew": "车组A", "stop_id": 4, "start_minute": 1500, "end_minute": 1560, "capacity": 50},
        ]
        result = evaluate_shifts([], shifts)
        self.assertEqual(result[1]["status"], "pending")
        self.assertEqual(result[2]["status"], "pending")
        self.assertIn("时段重叠", result[1]["note"])
        self.assertEqual(result[3]["status"], "confirmed")


class ShuttlePlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.db = Database(self.db_path)
        seed_demo(self.db)
        self.stops = {row["code"]: row["id"] for row in self.db.list_stops()}
        disruption = self.db.create_disruption(
            "planner-01",
            {"code": "D-SHUTTLE", "name": "夜间停运接驳", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"},
            "planner",
        )
        self.disruption_id = disruption["id"]
        self.version = disruption["draft_version_id"]
        self.db.add_change(self.version, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")

    def tearDown(self):
        self.tmp.cleanup()

    def _demand(self, stop, headcount, version=None):
        return self.db.add_shuttle_demand(version or self.version, "planner-01", {"stop_id": self.stops[stop], "headcount": headcount}, "planner")

    def _shift(self, crew, stop, start, end, capacity, version=None, actor="planner-01"):
        return self.db.add_shuttle_shift(version or self.version, actor,
                                         {"crew": crew, "stop_id": self.stops[stop], "start_minute": start, "end_minute": end, "capacity": capacity},
                                         "planner")

    def _publish(self, version):
        self.db.transition(version, "planner-01", "planner", "submit")
        self.db.transition(version, "reviewer-01", "reviewer", "approve")
        return self.db.transition(version, "reviewer-01", "reviewer", "publish")

    def test_confirmed_shift_covers_closure_gap_and_publish_writes_snapshot(self):
        self._demand("S4", 40)
        report = self._shift("车组A", "S4", 1320, 1560, 45)
        self.assertEqual(report["plan_status"], "confirmed")
        self.assertEqual(report["shifts"][0]["status"], "confirmed")
        self.assertEqual(report["shifts"][0]["start_clock"], "22:00")
        self.assertEqual(report["shifts"][0]["end_clock"], "次日02:00")
        self.assertTrue(report["demands"][0]["closed"])
        self.assertTrue(report["demands"][0]["covered"])
        self.assertEqual(report["gaps"], [])

        published = self._publish(self.version)
        snapshot = json.loads(published["snapshot"])
        self.assertEqual(snapshot["shuttle"]["plan_status"], "confirmed")
        self.assertEqual(snapshot["shuttle"]["demands"], [{"stop_id": self.stops["S4"], "stop_code": "S4", "headcount": 40}])
        self.assertEqual(len(snapshot["shuttle"]["confirmed_shifts"]), 1)
        self.assertEqual(snapshot["shuttle"]["confirmed_shifts"][0]["crew"], "车组A")
        self.assertEqual(snapshot["shuttle"]["pending_shifts"], 0)

    def test_crew_overlap_and_capacity_shortfall_stay_pending_with_gap_notes(self):
        self._demand("S4", 100)
        self._shift("车组A", "S4", 1320, 1440, 120)
        report = self._shift("车组A", "S4", 1400, 1500, 120)
        by_start = {s["start_minute"]: s for s in report["shifts"]}
        self.assertEqual(by_start[1320]["status"], "pending")
        self.assertEqual(by_start[1400]["status"], "pending")
        self.assertIn("时段重叠", by_start[1320]["note"])
        self.assertIn("时段重叠", by_start[1400]["note"])

        report = self._shift("车组B", "S4", 1320, 1500, 60)
        small = [s for s in report["shifts"] if s["crew"] == "车组B"][0]
        self.assertEqual(small["status"], "pending")
        self.assertIn("缺口 40 人", small["note"])
        # 没有已确认班次覆盖 S4，站点缺口出现在版本级报告里
        self.assertEqual(report["plan_status"], "pending")
        self.assertTrue(any("无人覆盖" in gap and "100 人" in gap for gap in report["gaps"]))

    def test_uncovered_station_is_listed_as_gap(self):
        self._demand("S4", 30)
        report = self.db.shuttle_coverage(self.version)
        self.assertEqual(report["plan_status"], "pending")
        self.assertEqual(report["demands"][0]["gap_headcount"], 30)
        self.assertEqual(report["gaps"], ["停运站点 码头 需求 30 人无人覆盖，缺口 30 人"])
        self.assertEqual(report["closed_stops"][0]["stop_code"], "S4")

    def test_demand_upsert_recomputes_shift_status(self):
        self._demand("S4", 20)
        report = self._shift("车组A", "S4", 1320, 1500, 25)
        self.assertEqual(report["shifts"][0]["status"], "confirmed")
        report = self._demand("S4", 30)
        self.assertEqual(report["shifts"][0]["status"], "pending")
        self.assertIn("缺口 5 人", report["shifts"][0]["note"])
        report = self._demand("S4", 25)
        self.assertEqual(report["shifts"][0]["status"], "confirmed")
        self.assertEqual(report["plan_status"], "confirmed")
        self.assertEqual(len(report["demands"]), 1)

    def test_draft_only_and_validation(self):
        with self.assertRaises(DomainError):
            self._demand("S4", 0)
        with self.assertRaises(DomainError):
            self._shift("车组A", "S4", 1500, 1400, 40)
        with self.assertRaises(DomainError):
            self.db.add_shuttle_shift(self.version, "planner-01", {"crew": "车组A", "stop_id": 9999, "start_minute": 0, "end_minute": 60, "capacity": 40}, "planner")
        with self.assertRaises(DomainError):
            self.db.add_shuttle_demand(self.version, "viewer", {"stop_id": self.stops["S4"], "headcount": 10}, "viewer")
        self.db.transition(self.version, "planner-01", "planner", "submit")
        with self.assertRaises(DomainError):
            self._demand("S4", 10)
        with self.assertRaises(DomainError):
            self._shift("车组A", "S4", 1320, 1440, 40)

    def test_version_copy_snapshot_isolation_and_reopen(self):
        self._demand("S4", 40)
        self._shift("车组A", "S4", 1320, 1500, 50)
        published = self._publish(self.version)
        snapshot = json.loads(published["snapshot"])
        self.assertEqual(len(snapshot["shuttle"]["confirmed_shifts"]), 1)

        v2 = self.db.create_version_copy(self.disruption_id, self.version, "planner-02", "planner")["id"]
        copied = self.db.shuttle_coverage(v2)
        self.assertEqual(len(copied["demands"]), 1)
        self.assertEqual(len(copied["shifts"]), 1)
        self.assertEqual(copied["plan_status"], "confirmed")

        # 草稿 v2 上制造冲突，只影响新版本
        self._shift("车组A", "S4", 1400, 1560, 50, version=v2, actor="planner-02")
        self.assertEqual(self.db.shuttle_coverage(v2)["plan_status"], "pending")
        old = self.db.shuttle_coverage(self.version)
        self.assertEqual(old["plan_status"], "confirmed")
        self.assertEqual(old["shifts"][0]["status"], "confirmed")

        # 重开数据库后仍可按版本查看覆盖情况和已发布快照
        reopened = Database(self.db_path)
        again = reopened.shuttle_coverage(self.version)
        self.assertEqual(again["plan_status"], "confirmed")
        self.assertEqual(again["demands"][0]["headcount"], 40)
        row = [v for v in reopened.list_versions() if v["id"] == self.version][0]
        persisted = json.loads(row["snapshot"])
        self.assertEqual(persisted["shuttle"]["confirmed_shifts"][0]["capacity"], 50)
        self.assertEqual(reopened.shuttle_coverage(v2)["plan_status"], "pending")


if __name__ == "__main__":
    unittest.main()
