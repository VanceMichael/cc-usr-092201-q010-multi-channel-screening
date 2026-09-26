"""读取并检查共享领域资料。"""

import json
from pathlib import Path

REQUIRED = {
    "domain",
    "version",
    "sample_id",
    "channels",
    "record_types",
    "workflow_states",
    "state_transitions",
    "facts",
    "invariants",
    "change_events",
    "region_coverage",
    "films",
    "licenses",
    "venues",
    "guests",
    "screenings",
    "adaptation_issues",
    "receipts",
    "sample",
}
COLLECTIONS = {"films", "licenses", "venues", "guests", "screenings", "adaptation_issues", "receipts"}


def load_domain(path: Path) -> dict:
    """返回结构完整、引用一致的 v2 领域资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not REQUIRED.issubset(value):
        missing = sorted(REQUIRED - value.keys())
        raise ValueError(f"领域资料缺少必要字段: {missing}")
    if value["domain"] != "multi-channel-screening":
        raise ValueError("领域标识不匹配")
    if value["version"] < 2:
        raise ValueError("需要 v2 或更新版本的领域资料")
    for name in ("channels", "record_types", "workflow_states"):
        if len(value[name]) < 4:
            raise ValueError(f"{name} 内容不完整")
    if len(value["facts"]) < 2 or len(value["invariants"]) < 5:
        raise ValueError("业务事实或不变量不完整")
    for name in COLLECTIONS:
        if not isinstance(value[name], list):
            raise ValueError(f"{name} 必须是记录列表")
    _check_references(value)
    return value


def _check_references(value: dict) -> None:
    """检查样例记录之间的主要引用。"""
    film_ids = {f["id"] for f in value["films"]}
    venue_ids = {v["id"] for v in value["venues"]}
    master_ids = {m["id"] for f in value["films"] for m in f["masters"]}
    screening_ids = {s["id"] for s in value["screenings"]}
    guest_ids = {g["id"] for g in value["guests"]}

    for lic in value["licenses"]:
        if lic["film"] not in film_ids:
            raise ValueError(f"授权 {lic['id']} 引用了不存在的影片")
    for s in value["screenings"]:
        if s["film"] not in film_ids:
            raise ValueError(f"场次 {s['id']} 引用了不存在的影片")
        if s["master"] not in master_ids:
            raise ValueError(f"场次 {s['id']} 引用了不存在的母版")
        if s["venue"] not in venue_ids:
            raise ValueError(f"场次 {s['id']} 引用了不存在的场地")
        if s["status"] not in value["workflow_states"]:
            raise ValueError(f"场次 {s['id']} 状态未登记")
        for gid in s.get("guest_ids", []):
            if gid not in guest_ids:
                raise ValueError(f"场次 {s['id']} 引用了不存在的嘉宾")
    for r in value["receipts"]:
        if r["screening"] not in screening_ids:
            raise ValueError(f"回执 {r['id']} 引用了不存在的场次")
