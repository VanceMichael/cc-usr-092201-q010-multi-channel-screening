"""多渠道电影展映编排校验器。

把 fixtures/domain.json 中的业务约定落成可执行校验：

* 提案阶段校验母版/字幕/分级/设备/授权四维（渠道、地域、时段、次数），
  不通过则不锁定任何资源，设备类问题自动生成技术组适配工单；
* 确认排期与锁定授权次数、场馆时段在同一事务内完成（全成或全不成）；
* 发布名额必须等于可售+惠民免费且不超过容量/限流；
* 嘉宾改期、户外取消、地域拦截、换厅、退票只牵动相关记录；
* 任何重排重新过一遍不变量，已锁定+已核销不得超过授权次数；
* 放映结束按实际播放母版、观众容量逐场核销，供版权方核对。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

GUEST_BUFFER = timedelta(minutes=30)
ACTIVE_STATES = {"待适配", "已排片", "可开放", "需调整", "放映中"}
ONLINE_CHANNEL = "线上"
OUTDOOR_CHANNEL = "户外"


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class Violation:
    """一条校验结论。tech_surface=True 时应提前分派技术组。"""

    code: str
    message: str
    screening_id: str | None = None
    tech_surface: bool = False

    def __str__(self) -> str:
        head = f"[{self.code}]"
        if self.screening_id:
            head += f" {self.screening_id}"
        return f"{head} {self.message}"


class SchedulingError(Exception):
    """确认或变更操作未通过校验，状态未发生任何改变。"""

    def __init__(self, violations: list[Violation]):
        self.violations = violations
        super().__init__("；".join(str(v) for v in violations))


@dataclass
class ProposalResult:
    ok: bool
    violations: list[Violation] = field(default_factory=list)
    issue_ids: list[str] = field(default_factory=list)


class Schedule:
    """内存态排期表；所有写操作先在副本上校验，通过才提交。"""

    def __init__(self, data: dict):
        self.data = copy.deepcopy(data)
        self.events: list[dict] = []
        self._issue_seq = len(data.get("adaptation_issues", []))

    # ---- 基础查找 ----------------------------------------------------------

    @classmethod
    def from_fixture(cls, path: str | Path = "fixtures/domain.json") -> "Schedule":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def _film(self, film_id: str) -> dict:
        return next(f for f in self.data["films"] if f["id"] == film_id)

    def _master(self, master_id: str) -> tuple[dict, dict]:
        for film in self.data["films"]:
            for master in film["masters"]:
                if master["id"] == master_id:
                    return film, master
        raise KeyError(f"母版不存在: {master_id}")

    def _venue(self, venue_id: str) -> dict:
        return next(v for v in self.data["venues"] if v["id"] == venue_id)

    def _screening(self, sid: str) -> dict:
        return next(s for s in self.data["screenings"] if s["id"] == sid)

    def _guest(self, gid: str) -> dict:
        return next(g for g in self.data["guests"] if g["id"] == gid)

    def _effective_capacity(self, screening: dict) -> int:
        venue = self._venue(screening["venue"])
        return min(screening.get("capacity_override", venue["capacity"]), venue["capacity"])

    def _coverage(self, label: str) -> set[str]:
        return set(self.data["region_coverage"].get(label, []))

    def _license(self, film_id: str) -> dict | None:
        return next((l for l in self.data["licenses"] if l["film"] == film_id), None)

    def _grant_covers(self, grant: dict, channel: str, region_label: str,
                      start: datetime, end: datetime) -> bool:
        """条款是否覆盖渠道/地域/时段三维（不含次数，次数由 INV-01 单独计）。"""
        if channel not in grant["channels"]:
            return False
        if region_label not in self.data["region_coverage"]:
            return False  # 境外等未登记地域永不覆盖
        allowed: set[str] = set()
        for r in grant["regions"]:
            allowed |= self._coverage(r)
        if not self._coverage(region_label).issubset(allowed):
            return False
        window = grant["window"]
        return _dt(window["start"]) <= start and end <= _dt(window["end"])

    def _matching_grant(self, screening: dict) -> dict | None:
        """返回覆盖该场次渠道/地域/时段的授权条款。"""
        lic = self._license(screening["film"])
        if lic is None:
            return None
        venue = self._venue(screening["venue"])
        start, end = _dt(screening["start"]), _dt(screening["end"])
        return next(
            (g for g in lic["grants"]
             if self._grant_covers(g, venue["channel"], screening["region"], start, end)),
            None,
        )

    def _grant_for(self, screening: dict) -> tuple[dict | None, dict | None]:
        return self._license(screening["film"]), self._matching_grant(screening)

    def _license_diagnose(self, screening: dict) -> list[Violation]:
        """三维都对不上时，指出究竟是渠道、地域还是时段越界。"""
        sid = screening["id"]
        lic = self._license(screening["film"])
        if lic is None:
            return [Violation("INV-02", "影片没有任何放映授权", sid)]
        venue = self._venue(screening["venue"])
        start, end = _dt(screening["start"]), _dt(screening["end"])
        if not any(venue["channel"] in g["channels"] for g in lic["grants"]):
            return [Violation("INV-02", f"授权不含{venue['channel']}渠道", sid)]
        for grant in lic["grants"]:
            if venue["channel"] not in grant["channels"]:
                continue
            if screening["region"] not in self.data["region_coverage"]:
                return [Violation("INV-03", f"地域 {screening['region']} 不在任何授权地域内（境外不覆盖）", sid)]
            allowed: set[str] = set()
            for r in grant["regions"]:
                allowed |= self._coverage(r)
            if not self._coverage(screening["region"]).issubset(allowed):
                return [Violation("INV-03", f"地域 {screening['region']} 超出授权地域 {grant['regions']}", sid)]
            window = grant["window"]
            if not (_dt(window["start"]) <= start and end <= _dt(window["end"])):
                return [Violation("INV-04", f"场次时间超出授权窗口 {window['start']}~{window['end']}", sid)]
        return [Violation("INV-02", "没有可覆盖该场次的授权条款", sid)]

    # ---- 权利计数（INV-01） -------------------------------------------------

    def rights_usage(self, grant_id: str) -> dict:
        """已锁定 = 活动未核销场次；已核销 = 回执记录。"""
        locked = 0
        for s in self.data["screenings"]:
            if s["status"] not in ACTIVE_STATES:
                continue
            grant = self._matching_grant(s)
            if grant and grant["id"] == grant_id:
                locked += 1
        consumed = 0
        for receipt in self.data["receipts"]:
            for used in receipt.get("rights", []):
                if used["grant"] == grant_id and receipt["status"] == "已核销":
                    consumed += used["screenings_consumed"]
        grant = next(g for l in self.data["licenses"] for g in l["grants"] if g["id"] == grant_id)
        return {
            "grant": grant_id,
            "max": grant["max_screenings"],
            "locked": locked,
            "consumed": consumed,
            "remaining": grant["max_screenings"] - locked - consumed,
        }

    # ---- 单场校验 -----------------------------------------------------------

    def validate_screening(self, screening: dict, *, ignore_id: str | None = None) -> list[Violation]:
        sid = screening.get("id")
        violations: list[Violation] = []
        film = self._film(screening["film"])
        _, master = self._master(screening["master"])
        venue = self._venue(screening["venue"])
        lic, grant = self._grant_for(screening)
        start, end = _dt(screening["start"]), _dt(screening["end"])

        # INV-02/03/04：授权渠道、地域、时段
        if grant is None:
            violations.extend(self._license_diagnose(screening))

        # INV-01：授权次数
        if grant:
            usage = self.rights_usage(grant["id"])
            existing = any(s["id"] == sid for s in self.data["screenings"])
            candidate_lock = 0 if existing else 1
            if usage["locked"] + usage["consumed"] + candidate_lock > grant["max_screenings"]:
                violations.append(Violation("INV-01", f"授权 {grant['id']} 仅剩 {usage['remaining']} 场，无法再锁定", sid))

        # INV-05：母版设备适配
        missing = [e for e in master["requires_equipment"] if e not in venue["equipment"]]
        if missing:
            violations.append(
                Violation("INV-05", f"母版 {master['id']} 需要 {missing}，场地 {venue['name']} 不具备", sid, tech_surface=True)
            )

        # INV-06：字幕
        available = {sub["language"] for sub in master["subtitles"]}
        missing_subs = [lang for lang in screening.get("required_subtitles", []) if lang not in available]
        if missing_subs:
            violations.append(Violation("INV-06", f"母版缺少字幕语种: {missing_subs}", sid, tech_surface=True))

        # INV-07：分级提示
        if not film.get("rating", {}).get("label"):
            violations.append(Violation("INV-07", "影片未绑定分级标签与提示语，不得开放发布", sid))

        # INV-08：场地时段不重叠
        for other in self.data["screenings"]:
            if other["id"] == sid or other["id"] == ignore_id or other["status"] == "已取消":
                continue
            if other["venue"] != screening["venue"]:
                continue
            if start < _dt(other["end"]) and _dt(other["start"]) < end:
                violations.append(Violation("INV-08", f"与同场地场次 {other['id']} 时段冲突", sid))

        # INV-09：嘉宾可用且不撞期（含转场缓冲）
        for gid in screening.get("guest_ids", []):
            guest = self._guest(gid)
            if not any(_dt(w["start"]) <= start and end <= _dt(w["end"]) for w in guest["available_windows"]):
                violations.append(Violation("INV-09", f"嘉宾 {guest['name']} 该时段不可用", sid))
            for other in self.data["screenings"]:
                if other["id"] == sid or other["id"] == ignore_id or other["status"] == "已取消":
                    continue
                if gid not in other.get("guest_ids", []):
                    continue
                if start < _dt(other["end"]) + GUEST_BUFFER and _dt(other["start"]) - GUEST_BUFFER < end:
                    violations.append(Violation("INV-09", f"嘉宾 {guest['name']} 与场次 {other['id']} 撞期", sid))

        # INV-10：名额
        capacity = self._effective_capacity(screening)
        free = screening.get("free_seats", 0)
        sold = sum(screening.get("sales_by_region", {}).values())
        if free < 0 or free > capacity:
            violations.append(Violation("INV-10", f"惠民名额 {free} 超出容量 {capacity}", sid))
        if sold > capacity - free:
            violations.append(Violation("INV-10", f"已售 {sold} 超过可售名额 {capacity - free}", sid))

        # 线上地域：已成交订单地域必须被授权覆盖（INV-03）
        if venue["channel"] == ONLINE_CHANNEL and grant:
            allowed = self._coverage(grant["regions"][0])
            for region, qty in screening.get("sales_by_region", {}).items():
                if qty and region not in allowed:
                    violations.append(Violation("INV-03", f"线上订单地域 {region} 不在授权可售地域内", sid))

        # 未解决的阻塞性适配问题
        for issue in self.data.get("adaptation_issues", []):
            if issue.get("screening") == sid and issue.get("blocking") and issue["status"] != "已解决":
                violations.append(Violation("INV-05", f"存在未解决的阻塞问题 {issue['id']}: {issue['summary']}", sid, tech_surface=True))

        return violations

    def audit(self) -> list[Violation]:
        """对全部活动场次与回执做一次完整不变量巡检。"""
        findings: list[Violation] = []
        for screening in self.data["screenings"]:
            if screening["status"] == "已取消":
                continue
            findings.extend(self.validate_screening(screening))
        # INV-14：已核销场次必须有可核对回执
        for screening in self.data["screenings"]:
            if screening["status"] != "已核销":
                continue
            receipt = next((r for r in self.data["receipts"] if r["screening"] == screening["id"]), None)
            if receipt is None:
                findings.append(Violation("INV-14", "已核销场次缺少放映回执", screening["id"]))
                continue
            known_master = any(
                m["id"] == receipt["played_master"]
                for f in self.data["films"] for m in f["masters"]
            )
            if not known_master:
                findings.append(Violation("INV-14", "回执记录的实际播放母版无法核对", screening["id"]))
            if receipt["attendees"] > receipt["capacity"]:
                findings.append(Violation("INV-14", "回执观众数超过容量", screening["id"]))
            if sum(u["screenings_consumed"] for u in receipt["rights"]) != 1:
                findings.append(Violation("INV-14", "一场放映必须恰好消耗一次权利", screening["id"]))
        return findings

    # ---- 提案与确认（INV-11 原子性） ----------------------------------------

    def propose(self, fields: dict) -> ProposalResult:
        """只校验不落表；设备不兼容时生成技术组工单，但不锁定任何资源。"""
        candidate = copy.deepcopy(fields)
        candidate.setdefault("sales_by_region", {})
        candidate.setdefault("guest_ids", [])
        candidate.setdefault("free_seats", 0)
        candidate["status"] = "待适配"
        violations = self.validate_screening(candidate)
        issue_ids: list[str] = []
        if violations:
            for v in violations:
                if v.tech_surface:
                    issue_ids.append(self._open_issue(candidate, v.message, blocking=False))
        return ProposalResult(ok=not violations, violations=violations, issue_ids=issue_ids)

    def confirm(self, fields: dict) -> str:
        """校验通过才把场次写入排期并锁定授权次数与场馆时段。"""
        candidate = copy.deepcopy(fields)
        candidate.setdefault("sales_by_region", {})
        candidate.setdefault("guest_ids", [])
        candidate.setdefault("free_seats", 0)
        candidate["status"] = "已排片"
        violations = self.validate_screening(candidate)
        if violations:
            raise SchedulingError(violations)
        self.data["screenings"].append(candidate)
        for gid in candidate.get("guest_ids", []):
            guest = self._guest(gid)
            if candidate["id"] not in guest["screening_ids"]:
                guest["screening_ids"].append(candidate["id"])
        self._emit("确认排期", candidate["id"], {"锁定授权": 1, "场地": candidate["venue"]})
        return candidate["id"]

    def publish(self, sid: str) -> None:
        """已排片 → 可开放：发布名额必须恰好等于容量（INV-10）。"""
        screening = self._screening(sid)
        if screening["status"] not in {"已排片", "需调整"}:
            raise SchedulingError([Violation("STATE", f"状态 {screening['status']} 不可发布", sid)])
        violations = self.validate_screening(screening)
        capacity = self._effective_capacity(screening)
        sold = sum(screening.get("sales_by_region", {}).values())
        if screening.get("free_seats", 0) + sold > capacity:
            violations.append(Violation("INV-10", "名额合计超过容量，不能发布", sid))
        if violations:
            raise SchedulingError(violations)
        screening["status"] = "可开放"
        screening["published_capacity"] = capacity
        self._emit("开放发布", sid, {"发布名额": capacity, "惠民免费": screening.get("free_seats", 0), "可售": capacity - screening.get("free_seats", 0)})

    # ---- 售票 / 退票 --------------------------------------------------------

    def sell(self, sid: str, region: str, qty: int) -> None:
        screening = self._screening(sid)
        if screening["status"] != "可开放":
            raise SchedulingError([Violation("STATE", f"场次未开放售票（当前 {screening['status']}）", sid)])
        venue = self._venue(screening["venue"])
        _, grant = self._grant_for(screening)
        if venue["channel"] == ONLINE_CHANNEL:
            if grant is None:
                raise SchedulingError([Violation("INV-02", "线上场次缺少有效授权", sid)])
            allowed: set[str] = set()
            for r in grant["regions"]:
                allowed |= self._coverage(r)
            if region not in allowed:
                raise SchedulingError([Violation("INV-03", f"地域 {region} 超出线上授权可售地域，订单拦截", sid)])
        capacity = self._effective_capacity(screening)
        sold = sum(screening.get("sales_by_region", {}).values())
        if sold + qty > capacity - screening.get("free_seats", 0):
            raise SchedulingError([Violation("INV-10", "可售名额不足", sid)])
        screening.setdefault("sales_by_region", {})[region] = screening["sales_by_region"].get(region, 0) + qty
        self._emit("售票", sid, {"地域": region, "数量": qty})

    def refund(self, sid: str, region: str, qty: int) -> None:
        """开场前退票：只释放座席占用，权利计数纹丝不动。"""
        screening = self._screening(sid)
        if screening["status"] in {"已核销", "已取消"}:
            raise SchedulingError([Violation("STATE", "该状态不可退票", sid)])
        current = screening.get("sales_by_region", {}).get(region, 0)
        if qty > current:
            raise SchedulingError([Violation("INV-10", f"地域 {region} 可退数量不足", sid)])
        screening["sales_by_region"][region] = current - qty
        self._emit("退票", sid, {"地域": region, "数量": qty, "授权计数变化": 0})

    # ---- 嘉宾改期：只动关联关系 ---------------------------------------------

    def detach_guest(self, sid: str, gid: str) -> None:
        screening, guest = self._screening(sid), self._guest(gid)
        screening["guest_ids"] = [g for g in screening.get("guest_ids", []) if g != gid]
        guest["screening_ids"] = [s for s in guest["screening_ids"] if s != sid]
        self._emit("嘉宾改期-解除", sid, {"嘉宾": gid, "场次排期": "不变", "授权锁定": "不变"})

    def attach_guest(self, sid: str, gid: str) -> None:
        screening = self._screening(sid)
        screening.setdefault("guest_ids", []).append(gid)
        violations = [v for v in self.validate_screening(screening) if v.code == "INV-09"]
        if violations:
            screening["guest_ids"].remove(gid)
            raise SchedulingError(violations)
        self._guest(gid)["screening_ids"].append(sid)
        self._emit("嘉宾改期-绑定", sid, {"嘉宾": gid})

    # ---- 适配问题 -----------------------------------------------------------

    def _open_issue(self, screening_like: dict, summary: str, *, blocking: bool) -> str:
        self._issue_seq += 1
        issue_id = f"ISSUE-{self._issue_seq:03d}"
        self.data.setdefault("adaptation_issues", []).append(
            {"id": issue_id, "screening": screening_like.get("id"), "summary": summary,
             "assigned_to": "技术组", "blocking": blocking, "status": "待处理"}
        )
        return issue_id

    def report_issue(self, sid: str, summary: str, *, blocking: bool = True) -> str:
        screening = self._screening(sid)
        issue_id = self._open_issue(screening, summary, blocking=blocking)
        if blocking and screening["status"] in {"已排片", "可开放"}:
            screening["status"] = "需调整"
            self._emit("阻塞问题-暂停售票", sid, {"问题": issue_id})
        return issue_id

    def resolve_issue(self, issue_id: str) -> None:
        issue = next(i for i in self.data["adaptation_issues"] if i["id"] == issue_id)
        issue["status"] = "已解决"
        sid = issue.get("screening")
        if sid:
            screening = self._screening(sid)
            if screening["status"] == "需调整":
                screening["status"] = "已排片"
                self._emit("问题解决-待重新发布", sid, {"问题": issue_id})

    # ---- 取消（户外天气） ----------------------------------------------------

    def cancel(self, sid: str, reason: str) -> dict:
        """释放授权与场地、解除嘉宾、全额退票；其他场次一律不动。"""
        screening = self._screening(sid)
        if screening["status"] in {"已核销", "已取消"}:
            raise SchedulingError([Violation("STATE", "该状态不可取消", sid)])
        refunds = dict(screening.get("sales_by_region", {}))
        guests = list(screening.get("guest_ids", []))
        grant = self._matching_grant(screening)
        screening["status"] = "已取消"
        screening["cancel_reason"] = reason
        screening["sales_by_region"] = {}
        for gid in guests:
            screening["guest_ids"].remove(gid)
            self._guest(gid)["screening_ids"] = [s for s in self._guest(gid)["screening_ids"] if s != sid]
        self._emit("场次取消", sid, {"原因": reason, "释放授权": grant["id"] if grant else None,
                                 "释放场地": screening["venue"], "退票": refunds, "解除嘉宾": guests})
        return {"refunds": refunds, "released_grant": grant["id"] if grant else None}

    # ---- 换厅 / 改期：重排不破权（INV-13） ----------------------------------

    def change_hall(self, sid: str, new_venue_id: str) -> None:
        screening = self._screening(sid)
        old_venue = screening["venue"]
        sold = sum(screening.get("sales_by_region", {}).values())
        new_venue = self._venue(new_venue_id)
        if screening.get("free_seats", 0) + sold > self._effective_capacity({**screening, "venue": new_venue_id}):
            raise SchedulingError([Violation("INV-10", f"新厅 {new_venue['name']} 容量容不下已有的免费与已售名额", sid)])
        screening["venue"] = new_venue_id
        screening.pop("published_capacity", None)
        violations = self.validate_screening(screening)
        if violations:
            screening["venue"] = old_venue
            raise SchedulingError(violations)
        if screening["status"] == "可开放":
            screening["status"] = "需调整"
        self._emit("换厅", sid, {"旧厅": old_venue, "新厅": new_venue_id, "授权次数": "不变"})

    def reschedule(self, sid: str, new_start: str, new_end: str) -> None:
        screening = self._screening(sid)
        old = (screening["start"], screening["end"])
        screening["start"], screening["end"] = new_start, new_end
        violations = self.validate_screening(screening)
        if violations:
            screening["start"], screening["end"] = old
            raise SchedulingError(violations)
        if screening["status"] == "可开放":
            screening["status"] = "需调整"
        self._emit("改期", sid, {"新时间": f"{new_start}~{new_end}"})

    # ---- 放映核销（INV-14） -------------------------------------------------

    def close_out(self, sid: str, played_master: str, attendees: int) -> dict:
        """实际播放后逐场核销：锁转消耗，产出供版权方核对的回执。"""
        screening = self._screening(sid)
        if screening["status"] not in {"可开放", "放映中"}:
            raise SchedulingError([Violation("STATE", f"状态 {screening['status']} 不可核销", sid)])
        film = self._film(screening["film"])
        if not any(m["id"] == played_master for m in film["masters"]):
            raise SchedulingError([Violation("INV-14", "实际播放母版不属于该影片，无法核对", sid)])
        capacity = self._effective_capacity(screening)
        if attendees > capacity:
            raise SchedulingError([Violation("INV-14", f"实际观众 {attendees} 超过容量 {capacity}", sid)])
        lic, grant = self._grant_for(screening)
        if grant is None:
            raise SchedulingError([Violation("INV-02", "找不到覆盖该场的授权条款，无法核销权利", sid)])
        receipt_id = f"R-{sid.lstrip('S')}"
        receipt = {
            "id": receipt_id,
            "screening": sid,
            "planned_master": screening["master"],
            "played_master": played_master,
            "substituted": played_master != screening["master"],
            "actual_start": screening["start"],
            "actual_end": screening["end"],
            "capacity": capacity,
            "attendees": attendees,
            "rights": [{"license": lic["id"], "grant": grant["id"], "screenings_consumed": 1}],
            "reported_to": lic["copyright_holder"],
            "status": "已核销",
        }
        self.data["receipts"].append(receipt)
        screening["status"] = "已核销"
        self._emit("放映核销", sid, {"回执": receipt_id, "消耗授权": grant["id"]})
        return receipt

    def reconcile(self, copyright_holder: str | None = None) -> list[dict]:
        """返回可逐场交给版权方核对的回执清单。"""
        rows = []
        for receipt in self.data["receipts"]:
            if copyright_holder and receipt["reported_to"] != copyright_holder:
                continue
            screening = self._screening(receipt["screening"])
            rows.append({
                "场次": receipt["screening"],
                "影片": screening["film"],
                "渠道": self._venue(screening["venue"])["channel"],
                "计划母版": receipt["planned_master"],
                "实际母版": receipt["played_master"],
                "是否替换": receipt["substituted"],
                "容量": receipt["capacity"],
                "观众数": receipt["attendees"],
                "消耗权利": receipt["rights"],
                "版权方": receipt["reported_to"],
            })
        return rows

    # ---- 事件流水 -----------------------------------------------------------

    def _emit(self, event: str, sid: str, detail: dict) -> None:
        self.events.append({"event": event, "screening": sid, "detail": detail})
