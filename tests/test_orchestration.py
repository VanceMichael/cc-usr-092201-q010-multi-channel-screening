import unittest
from datetime import datetime, timedelta

from orchestration import (
    Channel,
    FilmMaster,
    Guest,
    OrchestrationError,
    Orchestrator,
    ScreeningLicense,
    Session,
    SessionState,
    VenueEquipment,
)

WINDOW_START = datetime(2026, 10, 1)
WINDOW_END = datetime(2026, 11, 1)


def at(day, hour):
    return datetime(2026, 10, day, hour, 0, 0)


def make_master(film_id, **overrides):
    data = dict(
        title="尼罗河之子",
        container="DCP",
        video_codec="JPEG2000",
        audio_layout="5.1",
        resolution="4K",
        subtitle_languages=frozenset({"zh", "en"}),
        rating_notice="全年龄适宜",
        runtime_minutes=112,
    )
    data.update(overrides)
    return FilmMaster(film_id, **data)


def make_license(license_id, film_id, channels, max_screenings, regions=None):
    return ScreeningLicense(
        license_id=license_id,
        film_id=film_id,
        licensor="开罗影业",
        allowed_channels=frozenset(channels),
        max_screenings=max_screenings,
        regions=None if regions is None else frozenset(regions),
        valid_from=WINDOW_START,
        valid_until=WINDOW_END,
    )


def make_session(session_id, film_id, license_id, venue_id, start, **overrides):
    return Session(
        session_id=session_id,
        film_id=film_id,
        license_id=license_id,
        venue_id=venue_id,
        start=start,
        end=overrides.pop("end", start + timedelta(hours=2)),
        **overrides,
    )


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator()
        # F1: 规格完整的剧情片; F2: 缺字幕轨与分级提示; F3: 户外可播的小规格片
        self.orch.register_master(make_master("F1"))
        self.orch.register_master(make_master(
            "F2", title="旧港纪录片", container="ProRes", video_codec="ProRes422",
            audio_layout="2.0", resolution="1080p",
            subtitle_languages=frozenset(), rating_notice=None, runtime_minutes=96,
        ))
        self.orch.register_master(make_master(
            "F3", title="海岸短片集", container="MP4", video_codec="H.265",
            audio_layout="2.0", resolution="1080p",
            subtitle_languages=frozenset({"zh"}), runtime_minutes=85,
        ))
        # L1: F1 仅授权线下两场; L3/L4: 常规授权; L5: F3 线上三场且限定地域
        self.orch.register_license(make_license(
            "L1", "F1", {Channel.CINEMA, Channel.OUTDOOR, Channel.CAMPUS}, 2,
        ))
        self.orch.register_license(make_license(
            "L3", "F2", {Channel.CINEMA, Channel.CAMPUS}, 5,
        ))
        self.orch.register_license(make_license(
            "L4", "F3", set(Channel), 10,
        ))
        self.orch.register_license(make_license("L5", "F3", {Channel.ONLINE}, 3, {"华东", "华北"}))
        self.orch.register_venue(VenueEquipment(
            "V-CIN", "影城1号厅", Channel.CINEMA, "城东", 120,
            frozenset({"DCP", "MP4"}), frozenset({"JPEG2000", "H.265"}),
            frozenset({"5.1", "2.0"}), "4K",
        ))
        self.orch.register_venue(VenueEquipment(
            "V-OUT", "滨江露天场", Channel.OUTDOOR, "滨江", 300,
            frozenset({"MP4"}), frozenset({"H.264", "H.265"}),
            frozenset({"2.0"}), "1080p",
        ))
        self.orch.register_venue(VenueEquipment(
            "V-NET", "官方点播平台", Channel.ONLINE, "全网", 10000,
            frozenset({"MP4"}), frozenset({"H.264", "H.265"}),
            frozenset({"2.0", "5.1"}), "4K",
        ))
        self.orch.register_venue(VenueEquipment(
            "V-CAM", "大学礼堂A", Channel.CAMPUS, "大学城", 200,
            frozenset({"DCP", "MP4", "ProRes"}), frozenset({"JPEG2000", "ProRes422", "H.265"}),
            frozenset({"5.1", "2.0"}), "4K",
        ))
        self.orch.register_guest(Guest("G1", "导演甲", at(1, 0), at(4, 0)))

    def plan(self, *args, **kwargs):
        session = make_session(*args, **kwargs)
        self.orch.plan_session(session)
        return session

    def codes(self, issues):
        return {issue.code for issue in issues}

    # ---- 确认排期: 原子锁定 ----

    def test_confirm_locks_license_venue_and_quota_atomically(self):
        self.plan("S1", "F1", "L1", "V-CIN", at(2, 10))
        self.assertEqual(self.orch.confirm_session("S1"), [])
        self.assertEqual(self.orch.licenses["L1"].consumed, 1)
        self.assertEqual(self.orch.sessions["S1"].state, SessionState.SCHEDULED)
        quota = self.orch.quotas["S1"]
        self.assertEqual(quota.sellable + quota.benefit, 120)

        # 同厅时段冲突的场次确认失败, 授权次数不被牵连消耗
        self.plan("S2", "F1", "L1", "V-CIN", at(2, 11))
        issues = self.orch.confirm_session("S2")
        self.assertIn("VENUE_SLOT_TAKEN", self.codes(issues))
        self.assertEqual(self.orch.licenses["L1"].consumed, 1)
        self.assertEqual(self.orch.sessions["S2"].state, SessionState.PENDING_ADAPTATION)

    def test_license_screening_limit_cannot_be_exceeded(self):
        # 版权只准线下两场, 第三场确认即被拦下
        self.plan("S1", "F1", "L1", "V-CIN", at(2, 10))
        self.plan("S2", "F1", "L1", "V-CAM", at(2, 10))
        self.plan("S3", "F1", "L1", "V-CIN", at(2, 18))
        self.assertEqual(self.orch.confirm_session("S1"), [])
        self.assertEqual(self.orch.confirm_session("S2"), [])
        issues = self.orch.confirm_session("S3")
        self.assertIn("LICENSE_EXHAUSTED", self.codes(issues))
        self.assertEqual(self.orch.licenses["L1"].consumed, 2)

    # ---- 排期即校验: 问题提前暴露 ----

    def test_outdoor_equipment_incompatibility_reported_in_advance(self):
        # DCP/4K/5.1 母版无法在户外设备播放, 登记排期时即暴露
        issues = self.orch.plan_session(make_session("S1", "F1", "L1", "V-OUT", at(3, 19)))
        self.assertTrue(
            {"MASTER_CONTAINER", "MASTER_CODEC", "MASTER_AUDIO", "MASTER_RESOLUTION"}
            <= self.codes(issues)
        )
        self.assertEqual(self.orch.open_issues(), issues)
        self.assertNotEqual(self.orch.confirm_session("S1"), [])
        self.assertEqual(self.orch.licenses["L1"].consumed, 0)

    def test_missing_subtitles_and_rating_block_confirm(self):
        self.plan("S1", "F2", "L3", "V-CAM", at(3, 14), required_subtitles=frozenset({"zh"}))
        issues = self.orch.confirm_session("S1")
        self.assertTrue({"SUBTITLE_MISSING", "RATING_MISSING"} <= self.codes(issues))
        self.assertEqual(self.orch.licenses["L3"].consumed, 0)

    def test_unknown_venue_or_license_is_rejected(self):
        with self.assertRaises(OrchestrationError):
            self.orch.plan_session(make_session("S1", "F1", "L1", "V-NONE", at(2, 10)))
        with self.assertRaises(OrchestrationError):
            self.orch.plan_session(make_session("S2", "F1", "L-NONE", "V-CIN", at(2, 10)))

    # ---- 线上地域授权 ----

    def test_online_region_restriction(self):
        # F3 为 MP4 流媒体母版, 适配线上平台
        self.plan("S1", "F3", "L5", "V-NET", at(5, 20), region="华南")
        self.assertIn("LICENSE_REGION", self.codes(self.orch.confirm_session("S1")))

        self.plan("S2", "F3", "L5", "V-NET", at(5, 20), region="华东")
        self.plan("S3", "F3", "L5", "V-NET", at(6, 20), region="华北")
        self.assertEqual(self.orch.confirm_session("S2"), [])
        self.assertEqual(self.orch.confirm_session("S3"), [])

        # 地域授权收紧后, 仅波及落在授权地域之外的场次
        affected = self.orch.update_license_regions("L5", frozenset({"华北"}))
        self.assertEqual(affected, ["S2"])
        self.assertEqual(self.orch.sessions["S2"].state, SessionState.NEEDS_ADJUSTMENT)
        self.assertEqual(self.orch.sessions["S3"].state, SessionState.SCHEDULED)

    # ---- 局部调整 ----

    def test_guest_reschedule_flags_only_related_sessions(self):
        self.plan("S1", "F1", "L1", "V-CIN", at(2, 10), guest_ids=("G1",))
        self.plan("S2", "F1", "L1", "V-CAM", at(3, 10), guest_ids=("G1",))
        self.plan("S3", "F3", "L4", "V-CIN", at(2, 18))
        for session_id in ("S1", "S2", "S3"):
            self.assertEqual(self.orch.confirm_session(session_id), [])

        # 嘉宾改到 10/3-10/5: 10/2 的 S1 受影响, 其余不动
        affected = self.orch.reschedule_guest("G1", at(3, 0), at(5, 0))
        self.assertEqual(affected, ["S1"])
        self.assertEqual(self.orch.sessions["S1"].state, SessionState.NEEDS_ADJUSTMENT)
        self.assertEqual(self.orch.sessions["S2"].state, SessionState.SCHEDULED)
        self.assertEqual(self.orch.sessions["S3"].state, SessionState.SCHEDULED)

    def test_cancel_releases_license_and_venue(self):
        self.plan("S1", "F3", "L4", "V-OUT", at(6, 19))
        self.assertEqual(self.orch.confirm_session("S1"), [])
        self.assertEqual(self.orch.licenses["L4"].consumed, 1)

        # 户外场次因天气取消, 授权次数与场馆时段即刻释放
        self.orch.cancel_session("S1", "暴雨橙色预警")
        self.assertEqual(self.orch.sessions["S1"].state, SessionState.CANCELLED)
        self.assertEqual(self.orch.licenses["L4"].consumed, 0)
        self.assertNotIn("S1", self.orch.quotas)

        self.plan("S2", "F3", "L4", "V-OUT", at(6, 19))
        self.assertEqual(self.orch.confirm_session("S2"), [])
        self.assertEqual(self.orch.licenses["L4"].consumed, 1)

    def test_change_venue_moves_locks_without_extra_consumption(self):
        self.plan("S1", "F1", "L1", "V-CIN", at(7, 10))
        self.assertEqual(self.orch.confirm_session("S1"), [])

        # 换到兼容的校园礼堂: 授权不重复消耗, 名额随新厅容量调整
        self.assertEqual(self.orch.change_venue("S1", "V-CAM"), [])
        self.assertEqual(self.orch.licenses["L1"].consumed, 1)
        self.assertEqual(self.orch.quotas["S1"].capacity, 200)

        # 原厅时段已释放, 新厅时段已锁住
        self.plan("S2", "F1", "L1", "V-CIN", at(7, 10))
        self.assertEqual(self.orch.confirm_session("S2"), [])
        self.plan("S3", "F3", "L4", "V-CAM", at(7, 11))
        self.assertIn("VENUE_SLOT_TAKEN", self.codes(self.orch.confirm_session("S3")))

        # 换到设备不兼容的户外场: 留在原厅并转为需调整
        issues = self.orch.change_venue("S1", "V-OUT")
        self.assertIn("MASTER_CONTAINER", self.codes(issues))
        self.assertEqual(self.orch.sessions["S1"].venue_id, "V-CAM")
        self.assertEqual(self.orch.sessions["S1"].state, SessionState.NEEDS_ADJUSTMENT)
        self.assertEqual(self.orch.licenses["L1"].consumed, 2)

    def test_reschedule_cannot_break_license_window(self):
        self.plan("S1", "F1", "L1", "V-CIN", at(10, 10))
        self.assertEqual(self.orch.confirm_session("S1"), [])

        # 改到授权时段之外: 被拒绝且场次时间不变
        issues = self.orch.reschedule_session("S1", datetime(2026, 11, 5, 10), datetime(2026, 11, 5, 12))
        self.assertIn("LICENSE_WINDOW", self.codes(issues))
        self.assertEqual(self.orch.sessions["S1"].start, at(10, 10))
        self.assertEqual(self.orch.sessions["S1"].state, SessionState.NEEDS_ADJUSTMENT)

        # 回到授权时段内重新确认, 授权不重复消耗
        self.assertEqual(self.orch.confirm_session("S1"), [])
        self.assertEqual(self.orch.licenses["L1"].consumed, 1)

        # 授权时段内改期成功, 原时段随之释放
        self.assertEqual(self.orch.reschedule_session("S1", at(12, 10), at(12, 12)), [])
        self.plan("S2", "F1", "L1", "V-CIN", at(10, 10))
        self.assertEqual(self.orch.confirm_session("S2"), [])

    def test_master_failure_and_redelivery_flow(self):
        self.plan("S1", "F1", "L1", "V-CIN", at(8, 10))
        self.plan("S2", "F1", "L1", "V-CAM", at(8, 10))
        self.plan("S3", "F3", "L4", "V-CAM", at(8, 14))
        for session_id in ("S1", "S2", "S3"):
            self.assertEqual(self.orch.confirm_session(session_id), [])

        # 母版校验失败只牵动该影片的场次
        affected = self.orch.report_master_failure("F1", "母版声道校验未通过")
        self.assertEqual(sorted(affected), ["S1", "S2"])
        self.assertEqual(self.orch.sessions["S3"].state, SessionState.SCHEDULED)
        self.assertEqual(self.orch.licenses["L1"].consumed, 2)

        # 失败未解决前无法重新确认
        self.assertIn("MASTER_FAILED", self.codes(self.orch.confirm_session("S1")))

        # 新母版交付后重新确认, 授权不重复消耗
        self.orch.register_master(make_master("F1"))
        self.assertEqual(self.orch.confirm_session("S1"), [])
        self.assertEqual(self.orch.sessions["S1"].state, SessionState.SCHEDULED)
        self.assertEqual(self.orch.licenses["L1"].consumed, 2)
        self.assertEqual(self.orch.issues_for("S1"), [])

    # ---- 发布不变量与名额 ----

    def test_publish_invariant_holds(self):
        self.plan("S1", "F1", "L1", "V-CIN", at(9, 10), benefit_seats=20)
        with self.assertRaises(OrchestrationError):
            self.orch.open_session("S1")  # 未确认不能发布

        self.assertEqual(self.orch.confirm_session("S1"), [])
        self.orch.open_session("S1")
        self.assertEqual([s.session_id for s in self.orch.published_sessions()], ["S1"])
        self.assertEqual(self.orch.check_publish_invariant(), [])

        quota = self.orch.quotas["S1"]
        self.assertEqual((quota.sellable, quota.benefit), (100, 20))
        self.orch.record_sale("S1", 100)
        with self.assertRaises(OrchestrationError):
            self.orch.record_sale("S1", 1)  # 可售名额已售罄
        self.orch.claim_benefit("S1", 20)
        with self.assertRaises(OrchestrationError):
            self.orch.claim_benefit("S1", 1)  # 惠民名额已领完
        self.assertEqual(self.orch.check_publish_invariant(), [])

    def test_refund_only_touches_session_quota(self):
        self.plan("S1", "F1", "L1", "V-CIN", at(9, 10))
        self.assertEqual(self.orch.confirm_session("S1"), [])
        self.orch.open_session("S1")
        self.orch.record_sale("S1", 40)
        self.orch.refund("S1", 15)
        self.assertEqual(self.orch.quotas["S1"].sold, 25)
        with self.assertRaises(OrchestrationError):
            self.orch.refund("S1", 30)  # 超过已售数量
        self.assertEqual(self.orch.check_publish_invariant(), [])

    # ---- 放映核销与版权方对账 ----

    def test_reconciliation_and_licensor_report(self):
        self.plan("S1", "F1", "L1", "V-CIN", at(15, 10))
        self.plan("S2", "F1", "L1", "V-CAM", at(15, 10))
        for session_id in ("S1", "S2"):
            self.assertEqual(self.orch.confirm_session(session_id), [])
            self.orch.open_session(session_id)
            self.orch.start_screening(session_id)

        with self.assertRaises(OrchestrationError):
            self.orch.reconcile("S1", True, 500)  # 观众人数超出容量

        receipt1 = self.orch.reconcile("S1", True, 110)
        self.assertEqual((receipt1.actual_plays, receipt1.rights_consumed), (1, 1))
        self.assertEqual(self.orch.sessions["S1"].state, SessionState.RECONCILED)

        # 未实际播放的场次不消耗授权
        receipt2 = self.orch.reconcile("S2", False, 0)
        self.assertEqual(receipt2.rights_consumed, 0)
        self.assertEqual(self.orch.licenses["L1"].consumed, 1)

        report = self.orch.licensor_report("L1")
        self.assertEqual((report["granted"], report["consumed"], report["remaining"]), (2, 1, 1))
        rows = {row["session_id"]: row for row in report["sessions"]}
        self.assertEqual((rows["S1"]["played"], rows["S1"]["attendance"]), (True, 110))
        self.assertEqual(rows["S1"]["capacity"], 120)
        self.assertEqual(rows["S1"]["rights_consumed"], 1)
        self.assertEqual((rows["S2"]["played"], rows["S2"]["rights_consumed"]), (False, 0))
        self.assertEqual(sum(r["rights_consumed"] for r in rows.values()), report["consumed"])


if __name__ == "__main__":
    unittest.main()
