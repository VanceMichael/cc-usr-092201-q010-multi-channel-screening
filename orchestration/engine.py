"""多渠道展映编排器。

把影片母版、字幕、分级提示、场地设备、地域时段授权、映后嘉宾和惠民
座席的校验前置到排期阶段: 确认一场排期的同时原子锁住实际版权次数与
场馆资源; 母版失败、嘉宾改期、户外取消、地域收紧、换厅、退票等调整
只牵动相关场次, 且任何重排都不会突破授权; 放映结束后生成回执, 供
版权方逐场核对实际播放、观众容量与已消耗的权利。
"""

from __future__ import annotations

from datetime import datetime

from .models import (
    RESOLUTION_RANK,
    FilmMaster,
    Guest,
    Issue,
    ScreeningLicense,
    ScreeningReceipt,
    SeatQuota,
    Session,
    SessionState,
    VenueEquipment,
)

# 尚未放映、可能被局部调整波及的场次状态。
_ADJUSTABLE_STATES = (
    SessionState.PENDING_ADAPTATION,
    SessionState.SCHEDULED,
    SessionState.OPENABLE,
)

# 已锁定安排的场次状态; 校验不再通过时才转为需调整。
_FLAGGABLE_STATES = (
    SessionState.SCHEDULED,
    SessionState.OPENABLE,
)

# 允许执行确认排期的状态。
_CONFIRMABLE_STATES = (
    SessionState.PENDING_ADAPTATION,
    SessionState.NEEDS_ADJUSTMENT,
)

# 允许取消、换厅或改期的状态。
_CHANGEABLE_STATES = _ADJUSTABLE_STATES + (SessionState.NEEDS_ADJUSTMENT,)


class OrchestrationError(Exception):
    """违反流程状态或资源不变量时抛出。"""


class Orchestrator:
    """多渠道展映编排器。

    排期即整体校验, 确认即原子锁定; 局部调整只牵动相关场次; 发布名额
    恒等于可售与免费名额之和; 放映结束后向版权方提供逐场对账单。
    """

    def __init__(self) -> None:
        self.masters: dict[str, FilmMaster] = {}
        self.licenses: dict[str, ScreeningLicense] = {}
        self.venues: dict[str, VenueEquipment] = {}
        self.guests: dict[str, Guest] = {}
        self.sessions: dict[str, Session] = {}
        self.quotas: dict[str, SeatQuota] = {}
        self.receipts: dict[str, ScreeningReceipt] = {}
        self._issues: dict[str, list[Issue]] = {}
        # 场馆时段锁: 场地 -> [(场次, 开始, 结束)]
        self._venue_locks: dict[str, list[tuple[str, datetime, datetime]]] = {}
        # 校验未通过、等待重新交付的母版: 影片 -> 失败说明
        self._failed_masters: dict[str, str] = {}

    # ---- 资料登记 ----

    def register_master(self, master: FilmMaster) -> None:
        """登记或更新影片母版; 重新交付后此前的母版失败记录随之失效。"""
        self.masters[master.film_id] = master
        self._failed_masters.pop(master.film_id, None)

    def register_license(self, license_: ScreeningLicense) -> None:
        if license_.license_id in self.licenses:
            raise OrchestrationError(f"授权 {license_.license_id} 已登记")
        self.licenses[license_.license_id] = license_

    def register_venue(self, venue: VenueEquipment) -> None:
        if venue.venue_id in self.venues:
            raise OrchestrationError(f"场地 {venue.venue_id} 已登记")
        self.venues[venue.venue_id] = venue

    def register_guest(self, guest: Guest) -> None:
        if guest.guest_id in self.guests:
            raise OrchestrationError(f"嘉宾 {guest.guest_id} 已登记")
        self.guests[guest.guest_id] = guest

    # ---- 排期与确认 ----

    def plan_session(self, session: Session) -> list[Issue]:
        """登记一场排期并立即整体校验, 适配问题提前交给技术人员。

        场次板块以场地为准; 返回本次校验发现的适配问题。
        """
        if session.session_id in self.sessions:
            raise OrchestrationError(f"场次 {session.session_id} 已存在")
        venue = self.venues.get(session.venue_id)
        if venue is None:
            raise OrchestrationError(f"未知场地 {session.venue_id}")
        if session.license_id not in self.licenses:
            raise OrchestrationError(f"未知授权 {session.license_id}")
        if session.end <= session.start:
            raise OrchestrationError("结束时间必须晚于开始时间")
        session.channel = venue.channel
        session.state = SessionState.PENDING_ADAPTATION
        self.sessions[session.session_id] = session
        return self._refresh_issues(session)

    def validate_session(self, session_id: str) -> list[Issue]:
        """对一场排期做整体适配校验, 不改变任何状态。"""
        session = self.sessions[session_id]
        venue = self.venues[session.venue_id]
        license_ = self.licenses[session.license_id]
        master = self.masters.get(session.film_id)
        issues: list[Issue] = []

        if session.film_id in self._failed_masters:
            issues.append(Issue("MASTER_FAILED", self._failed_masters[session.film_id], session_id))
        if master is None:
            issues.append(Issue("MASTER_MISSING", "影片母版尚未交付", session_id))
        else:
            if master.container not in venue.containers:
                issues.append(Issue(
                    "MASTER_CONTAINER",
                    f"母版封装 {master.container} 与{venue.name}的设备不兼容",
                    session_id,
                ))
            if master.video_codec not in venue.codecs:
                issues.append(Issue(
                    "MASTER_CODEC",
                    f"母版编码 {master.video_codec} 无法在{venue.name}解码",
                    session_id,
                ))
            if master.audio_layout not in venue.audio_layouts:
                issues.append(Issue(
                    "MASTER_AUDIO",
                    f"母版声道 {master.audio_layout} 无法在{venue.name}还音",
                    session_id,
                ))
            master_rank = RESOLUTION_RANK.get(master.resolution)
            venue_rank = RESOLUTION_RANK.get(venue.max_resolution)
            if master_rank is None or venue_rank is None or master_rank > venue_rank:
                issues.append(Issue(
                    "MASTER_RESOLUTION",
                    f"母版分辨率 {master.resolution} 超出{venue.name}的能力 {venue.max_resolution}",
                    session_id,
                ))
            missing = set(session.required_subtitles) - set(master.subtitle_languages)
            if missing:
                issues.append(Issue(
                    "SUBTITLE_MISSING",
                    f"缺少字幕轨: {', '.join(sorted(missing))}",
                    session_id,
                ))
            if master.rating_notice is None:
                issues.append(Issue("RATING_MISSING", "影片尚未提供分级提示", session_id))

        if session.channel not in license_.allowed_channels:
            issues.append(Issue(
                "LICENSE_CHANNEL",
                f"{session.channel.value}板块超出授权范围",
                session_id,
            ))
        if license_.regions is not None:
            region = session.region or venue.region
            if region not in license_.regions:
                issues.append(Issue(
                    "LICENSE_REGION",
                    f"地域 {region} 超出授权地域",
                    session_id,
                ))
        if session.start < license_.valid_from or session.end > license_.valid_until:
            issues.append(Issue("LICENSE_WINDOW", "场次时段超出授权时段", session_id))
        if not session.license_locked and license_.remaining < 1:
            issues.append(Issue("LICENSE_EXHAUSTED", "授权放映次数已用尽", session_id))

        conflict = self._venue_conflict(session)
        if conflict is not None:
            issues.append(Issue("VENUE_SLOT_TAKEN", f"场地时段与场次 {conflict} 冲突", session_id))

        for guest_id in session.guest_ids:
            guest = self.guests.get(guest_id)
            if guest is None:
                issues.append(Issue("GUEST_UNKNOWN", f"映后嘉宾 {guest_id} 未登记", session_id))
            elif not (guest.available_from <= session.start and session.end <= guest.available_until):
                issues.append(Issue(
                    "GUEST_UNAVAILABLE",
                    f"嘉宾 {guest.name} 的档期无法覆盖该场次",
                    session_id,
                ))

        if not 0 <= session.benefit_seats <= venue.capacity:
            issues.append(Issue("BENEFIT_OVERFLOW", "惠民座席数量超出场地容量", session_id))
        return issues

    def confirm_session(self, session_id: str) -> list[Issue]:
        """确认排期: 校验全部通过后, 原子锁住一次授权与场馆时段。

        返回空列表表示确认成功; 否则返回问题清单, 且不产生任何锁定。
        从需调整重新确认时不会重复消耗授权次数。
        """
        session = self.sessions[session_id]
        self._require_state(session, *_CONFIRMABLE_STATES)
        issues = self._refresh_issues(session)
        if any(issue.blocker for issue in issues):
            return issues
        license_ = self.licenses[session.license_id]
        if not session.license_locked:
            license_.consumed += 1
            session.license_locked = True
        self._release_venue(session)
        self._lock_venue(session)
        if session_id not in self.quotas:
            venue = self.venues[session.venue_id]
            self.quotas[session_id] = SeatQuota(
                session_id=session_id,
                capacity=venue.capacity,
                sellable=venue.capacity - session.benefit_seats,
                benefit=session.benefit_seats,
            )
        session.state = SessionState.SCHEDULED
        return []

    def open_session(self, session_id: str) -> None:
        """发布场次: 对外开放的场次名额必须恒等于可售与免费名额之和。"""
        session = self.sessions[session_id]
        self._require_state(session, SessionState.SCHEDULED)
        quota = self.quotas.get(session_id)
        if quota is None:
            raise OrchestrationError("场次缺少座席名额, 不能发布")
        if quota.sellable + quota.benefit != quota.capacity:
            raise OrchestrationError("发布名额与场地容量不一致")
        session.state = SessionState.OPENABLE

    # ---- 售票与退票 ----

    def record_sale(self, session_id: str, count: int = 1) -> None:
        quota = self._quota(session_id)
        if count < 0 or quota.sold + count > quota.sellable:
            raise OrchestrationError("可售名额不足")
        quota.sold += count

    def claim_benefit(self, session_id: str, count: int = 1) -> None:
        quota = self._quota(session_id)
        if count < 0 or quota.benefit_claimed + count > quota.benefit:
            raise OrchestrationError("惠民名额不足")
        quota.benefit_claimed += count

    def refund(self, session_id: str, count: int = 1) -> None:
        """退票: 仅回补该场次的可售名额, 不影响其他安排。"""
        quota = self._quota(session_id)
        if count < 0 or count > quota.sold:
            raise OrchestrationError("退票数量超过已售数量")
        quota.sold -= count

    # ---- 局部调整 ----

    def report_master_failure(self, film_id: str, message: str) -> list[str]:
        """母版校验失败: 仅将该影片尚未放映的场次转为需调整。"""
        self._failed_masters[film_id] = message
        return self._flag_adjustable(
            session for session in self.sessions.values() if session.film_id == film_id
        )

    def reschedule_guest(
        self, guest_id: str, available_from: datetime, available_until: datetime
    ) -> list[str]:
        """嘉宾改期: 仅牵动新档期覆盖不了的相关场次。"""
        guest = self.guests[guest_id]
        guest.available_from = available_from
        guest.available_until = available_until
        return self._flag_adjustable(
            session for session in self.sessions.values() if guest_id in session.guest_ids
        )

    def update_license_regions(self, license_id: str, regions: frozenset[str] | None) -> list[str]:
        """收紧或调整地域授权: 仅波及落在授权地域之外的相关场次。"""
        license_ = self.licenses[license_id]
        license_.regions = regions
        return self._flag_adjustable(
            session for session in self.sessions.values() if session.license_id == license_id
        )

    def cancel_session(self, session_id: str, reason: str = "") -> None:
        """取消场次(如户外场次因天气取消): 释放已锁的授权次数与场馆资源。"""
        session = self.sessions[session_id]
        self._require_state(session, *_CHANGEABLE_STATES)
        self._release_locks(session)
        self.quotas.pop(session_id, None)
        self._issues.pop(session_id, None)
        session.state = SessionState.CANCELLED

    def change_venue(self, session_id: str, new_venue_id: str) -> list[Issue]:
        """换厅: 重新校验新厅设备与授权, 通过则平移场馆资源锁。

        授权次数不因换厅重复消耗; 校验失败时场次留在原厅并转为需调整,
        不产生任何资源变动。
        """
        session = self.sessions[session_id]
        self._require_state(session, *_CHANGEABLE_STATES)
        new_venue = self.venues.get(new_venue_id)
        if new_venue is None:
            raise OrchestrationError(f"未知场地 {new_venue_id}")
        old_venue_id = session.venue_id
        old_state = session.state
        session.venue_id = new_venue_id
        session.channel = new_venue.channel
        issues = self.validate_session(session_id)
        capacity_issue = self._capacity_issue(session, new_venue)
        if capacity_issue is not None:
            issues.append(capacity_issue)
        if any(issue.blocker for issue in issues):
            session.venue_id = old_venue_id
            session.channel = self.venues[old_venue_id].channel
            session.state = SessionState.NEEDS_ADJUSTMENT
            self._issues[session_id] = issues
            return issues
        if self._release_venue_at(old_venue_id, session_id):
            self._lock_venue(session)
        quota = self.quotas.get(session_id)
        if quota is not None:
            quota.capacity = new_venue.capacity
            quota.sellable = new_venue.capacity - session.benefit_seats
            quota.benefit = session.benefit_seats
        if old_state == SessionState.NEEDS_ADJUSTMENT:
            session.state = (
                SessionState.SCHEDULED if session.license_locked else SessionState.PENDING_ADAPTATION
            )
        self._issues[session_id] = issues
        return issues

    def reschedule_session(
        self, session_id: str, start: datetime, end: datetime
    ) -> list[Issue]:
        """场次改期: 重新校验授权时段、嘉宾档期与场地占用, 不得突破授权。"""
        session = self.sessions[session_id]
        self._require_state(session, *_CHANGEABLE_STATES)
        if end <= start:
            raise OrchestrationError("结束时间必须晚于开始时间")
        old_start, old_end = session.start, session.end
        old_state = session.state
        session.start, session.end = start, end
        issues = self.validate_session(session_id)
        if any(issue.blocker for issue in issues):
            session.start, session.end = old_start, old_end
            session.state = SessionState.NEEDS_ADJUSTMENT
            self._issues[session_id] = issues
            return issues
        locks = self._venue_locks.get(session.venue_id, [])
        self._venue_locks[session.venue_id] = [
            (sid, start, end) if sid == session_id else (sid, s, e)
            for sid, s, e in locks
        ]
        if old_state == SessionState.NEEDS_ADJUSTMENT:
            session.state = (
                SessionState.SCHEDULED if session.license_locked else SessionState.PENDING_ADAPTATION
            )
        self._issues[session_id] = issues
        return issues

    # ---- 放映与核销 ----

    def start_screening(self, session_id: str) -> None:
        session = self.sessions[session_id]
        self._require_state(session, SessionState.OPENABLE)
        session.state = SessionState.SCREENING

    def reconcile(self, session_id: str, played: bool, attendance: int) -> ScreeningReceipt:
        """放映结束核销: 生成回执, 供版权方逐场核对实际播放与已消耗权利。

        未实际播放的场次不消耗授权, 已锁住的次数随之释放。
        """
        session = self.sessions[session_id]
        self._require_state(session, SessionState.SCREENING)
        quota = self.quotas.get(session_id)
        capacity = quota.capacity if quota is not None else self.venues[session.venue_id].capacity
        if not 0 <= attendance <= capacity:
            raise OrchestrationError("观众人数超出场次容量")
        rights = 1 if played else 0
        if not played and session.license_locked:
            license_ = self.licenses[session.license_id]
            license_.consumed -= 1
            session.license_locked = False
        self._release_venue(session)
        receipt = ScreeningReceipt(
            session_id=session_id,
            film_id=session.film_id,
            license_id=session.license_id,
            played=played,
            actual_plays=rights,
            attendance=attendance,
            capacity=capacity,
            rights_consumed=rights,
        )
        self.receipts[session_id] = receipt
        self._issues.pop(session_id, None)
        session.state = SessionState.RECONCILED
        return receipt

    # ---- 发布不变量与对账 ----

    def published_sessions(self) -> list[Session]:
        """对外发布的场次: 可开放与放映中的场次。"""
        return [
            session
            for session in self.sessions.values()
            if session.state in (SessionState.OPENABLE, SessionState.SCREENING)
        ]

    def check_publish_invariant(self) -> list[str]:
        """核对发布不变量: 发布场次的可售与免费名额之和恒等于场地容量。"""
        problems: list[str] = []
        for session in self.published_sessions():
            quota = self.quotas.get(session.session_id)
            if quota is None:
                problems.append(f"场次 {session.session_id} 已发布但缺少座席名额")
                continue
            venue = self.venues[session.venue_id]
            if quota.sellable + quota.benefit != quota.capacity:
                problems.append(f"场次 {session.session_id} 的可售与免费名额之和不等于容量")
            if quota.capacity > venue.capacity:
                problems.append(f"场次 {session.session_id} 的名额超出场地容量")
            if not 0 <= quota.sold <= quota.sellable:
                problems.append(f"场次 {session.session_id} 的已售数量超出可售名额")
            if not 0 <= quota.benefit_claimed <= quota.benefit:
                problems.append(f"场次 {session.session_id} 的惠民领取数量超出免费名额")
        return problems

    def open_issues(self) -> list[Issue]:
        """尚未解决的适配问题台账, 供技术人员提前处理。"""
        return [issue for issues in self._issues.values() for issue in issues]

    def issues_for(self, session_id: str) -> list[Issue]:
        return list(self._issues.get(session_id, []))

    def licensor_report(self, license_id: str) -> dict:
        """版权方对账单: 逐场列出实际播放、观众容量与已消耗的权利。"""
        license_ = self.licenses[license_id]
        rows = []
        for session in self.sessions.values():
            if session.license_id != license_id:
                continue
            receipt = self.receipts.get(session.session_id)
            rows.append({
                "session_id": session.session_id,
                "channel": session.channel.value,
                "venue_id": session.venue_id,
                "start": session.start.isoformat(),
                "state": session.state.value,
                "played": receipt.played if receipt is not None else None,
                "attendance": receipt.attendance if receipt is not None else None,
                "capacity": receipt.capacity if receipt is not None else None,
                "rights_consumed": receipt.rights_consumed if receipt is not None else 0,
                "rights_locked": 1 if session.license_locked else 0,
            })
        return {
            "license_id": license_id,
            "licensor": license_.licensor,
            "film_id": license_.film_id,
            "granted": license_.max_screenings,
            "consumed": license_.consumed,
            "remaining": license_.remaining,
            "sessions": rows,
        }

    # ---- 内部工具 ----

    @staticmethod
    def _require_state(session: Session, *states: SessionState) -> None:
        if session.state not in states:
            allowed = "、".join(state.value for state in states)
            raise OrchestrationError(
                f"场次 {session.session_id} 当前为「{session.state.value}」, 不能执行该操作(需要: {allowed})"
            )

    def _quota(self, session_id: str) -> SeatQuota:
        quota = self.quotas.get(session_id)
        if quota is None:
            raise OrchestrationError(f"场次 {session_id} 尚未生成座席名额")
        return quota

    def _refresh_issues(self, session: Session) -> list[Issue]:
        issues = self.validate_session(session.session_id)
        self._issues[session.session_id] = issues
        return issues

    def _flag_adjustable(self, sessions) -> list[str]:
        """重新校验相关场次, 仅将已锁定安排且校验不再通过的转为需调整。"""
        affected = []
        for session in sessions:
            if session.state not in _ADJUSTABLE_STATES:
                continue
            issues = self._refresh_issues(session)
            if session.state in _FLAGGABLE_STATES and any(issue.blocker for issue in issues):
                session.state = SessionState.NEEDS_ADJUSTMENT
                affected.append(session.session_id)
        return affected

    def _venue_conflict(self, session: Session) -> str | None:
        for other_id, start, end in self._venue_locks.get(session.venue_id, []):
            if other_id != session.session_id and session.start < end and start < session.end:
                return other_id
        return None

    def _lock_venue(self, session: Session) -> None:
        self._venue_locks.setdefault(session.venue_id, []).append(
            (session.session_id, session.start, session.end)
        )

    def _release_venue(self, session: Session) -> bool:
        return self._release_venue_at(session.venue_id, session.session_id)

    def _release_venue_at(self, venue_id: str, session_id: str) -> bool:
        locks = self._venue_locks.get(venue_id, [])
        kept = [lock for lock in locks if lock[0] != session_id]
        self._venue_locks[venue_id] = kept
        return len(kept) != len(locks)

    def _release_locks(self, session: Session) -> None:
        if session.license_locked:
            license_ = self.licenses[session.license_id]
            license_.consumed -= 1
            session.license_locked = False
        self._release_venue(session)

    def _capacity_issue(self, session: Session, venue: VenueEquipment) -> Issue | None:
        quota = self.quotas.get(session.session_id)
        if quota is not None and venue.capacity - session.benefit_seats < quota.sold:
            return Issue("CAPACITY_SHRINK", "新厅容量装不下已售出的票", session.session_id)
        return None
