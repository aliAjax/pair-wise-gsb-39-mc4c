"""接驳计划规则：车组冲突、站点覆盖与运力缺口的纯函数计算。

规则模块不碰数据库和 HTTP，输入输出都是可 JSON 序列化的字典，
草稿编辑、发布快照和页面展示三处复用同一套结果。
"""
from __future__ import annotations

from typing import Any, Iterable

CONFIRMED = "confirmed"
PENDING = "pending"

STATION_COVERED = "covered"
STATION_PARTIAL = "partial"
STATION_UNCOVERED = "uncovered"
STATION_NO_DEMAND = "no_demand"


def fmt_clock(minutes: int) -> str:
    """服务日分钟格式化为 HH:MM，跨天加次日标记。"""
    day, minute = divmod(int(minutes), 1440)
    clock = f"{minute // 60:02d}:{minute % 60:02d}"
    if day == 1:
        return f"次日{clock}"
    if day > 1:
        return f"+{day}日{clock}"
    return clock


def windows_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """半开区间 [start, end) 是否重叠；首尾相接不算重叠。"""
    return int(a_start) < int(b_end) and int(b_start) < int(a_end)


def _window_text(start: int, end: int) -> str:
    return f"{fmt_clock(start)}~{fmt_clock(end)}"


def evaluate_plan(demands: Iterable[dict[str, Any]], trips: Iterable[dict[str, Any]],
                  affected_stop_ids: Iterable[int] = ()) -> dict[str, Any]:
    """评估一版接驳计划。

    需求含 id、stop_id、needed、start_minute/end_minute（可空，空表示全天）；
    班次含 id、label、crew、start_minute、end_minute、capacity、stop_ids。
    返回班次与需求的确认状态、站点覆盖汇总和缺口清单。
    覆盖与缺口只统计已确认班次（车组时段重叠的班次留在待确认，不计运力）。
    """
    trip_list = [dict(t) for t in trips]
    demand_list = [dict(d) for d in demands]

    # 规则一：同一车组的服务时段不能重叠，重叠双方一起留在待确认。
    trip_issues: dict[int, list[dict[str, Any]]] = {int(t["id"]): [] for t in trip_list}
    by_crew: dict[str, list[dict[str, Any]]] = {}
    for trip in trip_list:
        by_crew.setdefault(str(trip["crew"]).strip(), []).append(trip)
    for crew, members in by_crew.items():
        members.sort(key=lambda t: (int(t["start_minute"]), int(t["end_minute"]), int(t["id"])))
        for index, first in enumerate(members):
            for second in members[index + 1:]:
                if int(second["start_minute"]) >= int(first["end_minute"]):
                    break
                message = (f"车组{crew}时段重叠：{first['label']} "
                           f"{_window_text(first['start_minute'], first['end_minute'])} 与 "
                           f"{second['label']} {_window_text(second['start_minute'], second['end_minute'])}")
                trip_issues[int(first["id"])].append(
                    {"code": "crew_overlap", "message": message, "other_trip_id": int(second["id"])})
                trip_issues[int(second["id"])].append(
                    {"code": "crew_overlap", "message": message, "other_trip_id": int(first["id"])})

    trip_eval = {tid: {"status": PENDING if issues else CONFIRMED, "issues": issues}
                 for tid, issues in trip_issues.items()}
    confirmed_trips = [t for t in trip_list if trip_eval[int(t["id"])]["status"] == CONFIRMED]

    # 规则二、三：站点没人覆盖、单班容量不够的需求留在待确认并写明缺口。
    demand_eval: dict[int, dict[str, Any]] = {}
    for demand in demand_list:
        did = int(demand["id"])
        needed = int(demand["needed"])
        stop_id = int(demand["stop_id"])
        d_start, d_end = demand.get("start_minute"), demand.get("end_minute")
        covering = []
        for trip in confirmed_trips:
            if stop_id not in {int(s) for s in trip["stop_ids"]}:
                continue
            if d_start is not None and not windows_overlap(d_start, d_end, trip["start_minute"], trip["end_minute"]):
                continue
            covering.append(trip)
        covered = sum(int(t["capacity"]) for t in covering)
        gap = max(0, needed - covered)
        issues: list[dict[str, Any]] = []
        if not covering:
            issues.append({"code": "no_coverage", "message": f"站点没人覆盖：需求 {needed} 人，缺口 {needed} 人"})
        elif gap:
            issues.append({"code": "capacity_shortage",
                           "message": f"单班容量不够：需求 {needed} 人，已确认运力 {covered} 人，缺口 {gap} 人"})
        demand_eval[did] = {
            "status": PENDING if issues else CONFIRMED,
            "covered_capacity": covered,
            "gap": gap,
            "covering_trip_ids": [int(t["id"]) for t in covering],
            "covering_labels": [str(t["label"]) for t in covering],
            "issues": issues,
        }

    # 停运/跳站形成的站点缺口：逐站汇总需求、已确认运力和缺口。
    stations = []
    for stop_id in [int(s) for s in affected_stop_ids]:
        stop_demands = [d for d in demand_list if int(d["stop_id"]) == stop_id]
        needed = sum(int(d["needed"]) for d in stop_demands)
        covered = sum(min(demand_eval[int(d["id"])]["covered_capacity"], int(d["needed"])) for d in stop_demands)
        gap = sum(demand_eval[int(d["id"])]["gap"] for d in stop_demands)
        labels = sorted({label for d in stop_demands for label in demand_eval[int(d["id"])]["covering_labels"]})
        if not stop_demands:
            status = STATION_NO_DEMAND
        elif gap == 0:
            status = STATION_COVERED
        elif covered == 0:
            status = STATION_UNCOVERED
        else:
            status = STATION_PARTIAL
        stations.append({"stop_id": stop_id, "demand_count": len(stop_demands), "needed": needed,
                         "covered": covered, "gap": gap, "status": status, "covering_labels": labels})

    gaps: list[dict[str, Any]] = []
    for demand in demand_list:
        ev = demand_eval[int(demand["id"])]
        if not ev["issues"]:
            continue
        gaps.append({"kind": "demand", "demand_id": int(demand["id"]), "stop_id": int(demand["stop_id"]),
                     "needed": int(demand["needed"]), "covered": ev["covered_capacity"], "gap": ev["gap"],
                     "reason": ev["issues"][0]["code"], "message": ev["issues"][0]["message"]})
    for station in stations:
        if station["status"] == STATION_NO_DEMAND:
            gaps.append({"kind": "station", "stop_id": station["stop_id"], "needed": 0, "covered": 0,
                         "gap": 0, "reason": STATION_NO_DEMAND, "message": "停运站点未登记接驳需求"})

    total_needed = sum(int(d["needed"]) for d in demand_list)
    total_gap = sum(ev["gap"] for ev in demand_eval.values())
    summary = {
        "total_needed": total_needed,
        "total_covered": total_needed - total_gap,
        "total_gap": total_gap,
        "trips_total": len(trip_list),
        "trips_confirmed": sum(1 for ev in trip_eval.values() if ev["status"] == CONFIRMED),
        "trips_pending": sum(1 for ev in trip_eval.values() if ev["status"] == PENDING),
        "demands_total": len(demand_list),
        "demands_pending": sum(1 for ev in demand_eval.values() if ev["status"] == PENDING),
        "affected_stations": len(stations),
        "stations_with_gap": sum(1 for s in stations if s["status"] != STATION_COVERED),
    }
    return {"trips": trip_eval, "demands": demand_eval, "stations": stations,
            "gaps": gaps, "summary": summary}
