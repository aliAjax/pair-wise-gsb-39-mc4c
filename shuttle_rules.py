"""接驳计划台的纯规则：需求登记校验、班次冲突评估和站点覆盖汇总。

只处理普通字典和列表，不接触数据库，方便单独测试。
"""
from __future__ import annotations

from typing import Any

MAX_SERVICE_MINUTE = 2880  # 服务日分钟允许跨日，与班次时间口径一致


class RuleViolation(ValueError):
    """接驳规则校验失败，status 供接口层映射 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise RuleViolation(f"{field} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RuleViolation(f"{field} 必须是整数") from None


def normalize_demand(payload: dict[str, Any]) -> dict[str, int]:
    """校验站点人数需求：stop_id 必填，人数为正整数。"""
    if not isinstance(payload, dict):
        raise RuleViolation("需求必须是对象")
    if payload.get("stop_id") is None:
        raise RuleViolation("需求需要 stop_id")
    stop_id = _as_int(payload.get("stop_id"), "stop_id")
    headcount = _as_int(payload.get("headcount"), "headcount")
    if headcount < 1:
        raise RuleViolation("站点人数需求必须为正整数")
    return {"stop_id": stop_id, "headcount": headcount}


def normalize_shift(payload: dict[str, Any]) -> dict[str, Any]:
    """校验接驳班次：车组、站点、服务时段和载客量。"""
    if not isinstance(payload, dict):
        raise RuleViolation("班次必须是对象")
    crew = str(payload.get("crew", "")).strip()
    if not crew:
        raise RuleViolation("班次需要车组 crew")
    if payload.get("stop_id") is None:
        raise RuleViolation("班次需要 stop_id")
    stop_id = _as_int(payload.get("stop_id"), "stop_id")
    start = _as_int(payload.get("start_minute"), "start_minute")
    end = _as_int(payload.get("end_minute"), "end_minute")
    capacity = _as_int(payload.get("capacity"), "capacity")
    if start < 0 or end <= start or end > MAX_SERVICE_MINUTE:
        raise RuleViolation("服务时段不合法：需 0 <= 开始 < 结束 <= 2880 分钟")
    if capacity < 1:
        raise RuleViolation("载客量必须为正整数")
    return {"crew": crew, "stop_id": stop_id, "start_minute": start, "end_minute": end, "capacity": capacity}


def windows_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """半开区间重叠判断；首尾相接不算重叠。"""
    return a_start < b_end and b_start < a_end


def clock_label(minutes: int) -> str:
    """服务日分钟转时钟文案，跨日加“次日”前缀。"""
    day, minute = divmod(int(minutes), 1440)
    label = f"{minute // 60:02d}:{minute % 60:02d}"
    return f"次日{label}" if day else label


def evaluate_shifts(demands: list[dict[str, Any]], shifts: list[dict[str, Any]]) -> dict[Any, dict[str, str]]:
    """评估同一版本内的全部班次，返回 {班次id: {"status", "note"}}。

    规则：
    - 同一车组服务时段重叠 → 双方都留在待确认；
    - 单班载客量小于站点登记需求 → 待确认并写明缺口人数。
    """
    demand_by_stop = {int(d["stop_id"]): int(d["headcount"]) for d in demands}
    result: dict[Any, dict[str, str]] = {}
    for shift in shifts:
        reasons: list[str] = []
        need = demand_by_stop.get(int(shift["stop_id"]), 0)
        capacity = int(shift["capacity"])
        if capacity < need:
            reasons.append(f"载客量 {capacity} 人小于站点需求 {need} 人，缺口 {need - capacity} 人")
        for other in shifts:
            if other["id"] == shift["id"] or str(other["crew"]) != str(shift["crew"]):
                continue
            if windows_overlap(int(shift["start_minute"]), int(shift["end_minute"]),
                               int(other["start_minute"]), int(other["end_minute"])):
                reasons.append(f"车组 {shift['crew']} 与班次 #{other['id']} 时段重叠")
        result[shift["id"]] = {"status": "pending" if reasons else "confirmed", "note": "；".join(reasons)}
    return result


def build_coverage(demands: list[dict[str, Any]], shifts: list[dict[str, Any]],
                   closed_stop_ids: set[int], stops: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """汇总版本覆盖情况：需求覆盖、待确认班次和站点缺口。

    站点缺口按版本内 stop_closure 变更标记；登记了需求但没有已确认班次
    覆盖的站点会写入 gaps，整个版本计划保持待确认。
    """
    evaluations = evaluate_shifts(demands, shifts)
    confirmed_by_stop: dict[int, list[dict[str, Any]]] = {}
    shift_rows: list[dict[str, Any]] = []
    pending = 0
    for shift in shifts:
        evaluation = evaluations[shift["id"]]
        row = {**shift, "status": evaluation["status"], "note": evaluation["note"]}
        shift_rows.append(row)
        if evaluation["status"] == "confirmed":
            confirmed_by_stop.setdefault(int(shift["stop_id"]), []).append(row)
        else:
            pending += 1
    demand_rows: list[dict[str, Any]] = []
    gaps: list[str] = []
    for demand in demands:
        stop_id = int(demand["stop_id"])
        stop = stops.get(stop_id, {})
        headcount = int(demand["headcount"])
        covering = confirmed_by_stop.get(stop_id, [])
        covered = bool(covering)
        closed = stop_id in closed_stop_ids
        row = {
            "stop_id": stop_id,
            "stop_code": stop.get("code", str(stop_id)),
            "stop_name": stop.get("name", ""),
            "headcount": headcount,
            "closed": closed,
            "covered": covered,
            "confirmed_shifts": len(covering),
            "gap_headcount": 0 if covered else headcount,
        }
        demand_rows.append(row)
        if not covered:
            label = "停运站点" if closed else "站点"
            name = row["stop_name"] or row["stop_code"]
            gaps.append(f"{label} {name} 需求 {headcount} 人无人覆盖，缺口 {headcount} 人")
    return {
        "plan_status": "pending" if pending or gaps else "confirmed",
        "demands": demand_rows,
        "shifts": shift_rows,
        "gaps": gaps,
        "pending_shifts": pending,
    }
