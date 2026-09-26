"""多渠道编排场景测试：覆盖排片确认、变更牵动与逐场核销。"""

import unittest
from pathlib import Path

from domain_context.loader import load_domain
from domain_context.scheduling import Schedule, SchedulingError

FIXTURE = Path("fixtures/domain.json")


def codes(violations):
    return {v.code for v in violations}


class FixtureTest(unittest.TestCase):
    def test_bundled_schedule_is_clean(self):
        """样例自身必须通过全部不变量巡检。"""
        schedule = Schedule.from_fixture(FIXTURE)
        self.assertEqual(schedule.audit(), [])

    def test_rights_usage_in_sample(self):
        schedule = Schedule.from_fixture(FIXTURE)
        usage = schedule.rights_usage("G-F001-LINE")
        self.assertEqual((usage["locked"], usage["consumed"], usage["max"]), (1, 0, 2))
        out = schedule.rights_usage("G-F003-OUT")  # S0901 已实际放映
        self.assertEqual((out["locked"], out["consumed"]), (0, 1))


class ProposalAndConfirmTest(unittest.TestCase):
    def setUp(self):
        self.schedule = Schedule.from_fixture(FIXTURE)

    def test_master_incompatible_with_outdoor_raises_tech_issue_without_locking(self):
        """DCP 母版无法在户外设备播放：提案即暴露，生成技术工单，不锁任何资源。"""
        before = self.schedule.rights_usage("G-F001-OUT")
        result = self.schedule.propose({
            "id": "S2001", "film": "F001", "master": "M-F001-DCP",
            "venue": "V-RIVERSIDE", "start": "2026-10-14T19:30", "end": "2026-10-14T21:00",
            "region": "本市", "required_subtitles": ["中文"],
        })
        self.assertFalse(result.ok)
        self.assertIn("INV-05", codes(result.violations))
        self.assertEqual(len(result.issue_ids), 1)
        issue = next(i for i in self.schedule.data["adaptation_issues"] if i["id"] == result.issue_ids[0])
        self.assertEqual(issue["assigned_to"], "技术组")
        # 提案失败：场次未入表，授权次数与场地均未锁定
        self.assertFalse(any(s["id"] == "S2001" for s in self.schedule.data["screenings"]))
        self.assertEqual(self.schedule.rights_usage("G-F001-OUT"), before)

    def test_license_caps_at_two_offline_screenings(self):
        """版权只准线下两场：第三次锁定必须被拒绝。"""
        fields = {
            "film": "F001", "master": "M-F001-DCP", "venue": "V-CINEMA1",
            "start": "2026-10-15T19:00", "end": "2026-10-15T21:00",
            "region": "本市", "required_subtitles": ["中文"],
        }
        # S1001 已占 1 场，再确认 1 场达到上限 2
        self.schedule.confirm({**fields, "id": "S2101"})
        self.assertEqual(self.schedule.rights_usage("G-F001-LINE")["remaining"], 0)
        # 第三场被拒，且排期表没有它
        with self.assertRaises(SchedulingError) as ctx:
            self.schedule.confirm({**fields, "id": "S2102",
                                   "start": "2026-10-16T19:00", "end": "2026-10-16T21:00"})
        self.assertIn("INV-01", codes(ctx.exception.violations))
        self.assertFalse(any(s["id"] == "S2102" for s in self.schedule.data["screenings"]))

    def test_offline_only_license_cannot_go_online(self):
        result = self.schedule.propose({
            "id": "S2002", "film": "F001", "master": "M-F001-PRORES",
            "venue": "V-ONLINE", "start": "2026-10-14T20:00", "end": "2026-10-14T21:30",
            "region": "本市", "required_subtitles": ["中文"],
        })
        self.assertFalse(result.ok)
        self.assertIn("INV-02", codes(result.violations))

    def test_missing_subtitle_language(self):
        result = self.schedule.propose({
            "id": "S2003", "film": "F002", "master": "M-F002-STREAM",
            "venue": "V-ONLINE", "start": "2026-10-15T20:00", "end": "2026-10-15T21:30",
            "region": "中国大陆", "required_subtitles": ["英文"],
        })
        self.assertIn("INV-06", codes(result.violations))

    def test_window_violation_blocks_confirmation(self):
        with self.assertRaises(SchedulingError) as ctx:
            self.schedule.confirm({
                "id": "S2004", "film": "F005", "master": "M-F005-DCP",
                "venue": "V-CINEMA3", "start": "2026-11-01T19:00", "end": "2026-11-01T20:30",
                "region": "本市", "required_subtitles": ["中文"],
            })
        self.assertIn("INV-04", codes(ctx.exception.violations))

    def test_confirm_is_atomic(self):
        """同时撞设备与次数上限时，任何东西都不得落表。"""
        n_before = len(self.schedule.data["screenings"])
        with self.assertRaises(SchedulingError):
            self.schedule.confirm({
                "id": "S2005", "film": "F001", "master": "M-F001-DCP",
                "venue": "V-ROOM-B",  # 只有笔记本+投影仪
                "start": "2026-10-16T19:00", "end": "2026-10-16T21:00",
                "region": "本市", "required_subtitles": ["中文"],
            })
        self.assertEqual(len(self.schedule.data["screenings"]), n_before)

    def test_free_seats_over_capacity_rejected_at_confirmation(self):
        """惠民名额本身不能超过场馆容量，确认排期即拦截。"""
        with self.assertRaises(SchedulingError) as ctx:
            self.schedule.confirm({
                "id": "S2006", "film": "F004", "master": "M-F004-DCP",
                "venue": "V-HALL-A", "start": "2026-10-18T10:00", "end": "2026-10-18T11:30",
                "region": "本市", "required_subtitles": ["中文"], "free_seats": 500,
            })  # 惠民 500 > 礼堂容量 420
        self.assertIn("INV-10", codes(ctx.exception.violations))
        self.assertFalse(any(s["id"] == "S2006" for s in self.schedule.data["screenings"]))

    def test_published_capacity_equals_split(self):
        """发布总名额 = 可售 + 惠民免费 = 容量，并记录在场次上。"""
        self.schedule.confirm({
            "id": "S2007", "film": "F004", "master": "M-F004-DCP",
            "venue": "V-HALL-A", "start": "2026-10-18T10:00", "end": "2026-10-18T11:30",
            "region": "本市", "required_subtitles": ["中文"], "free_seats": 42,
        })
        self.schedule.publish("S2007")
        s2007 = self.schedule._screening("S2007")
        self.assertEqual(s2007["published_capacity"], 420)
        self.assertEqual(s2007["free_seats"], 42)  # 可售名额 = 420 - 42 = 378


class OnlineGeoTest(unittest.TestCase):
    def setUp(self):
        self.schedule = Schedule.from_fixture(FIXTURE)

    def test_nationwide_title_sells_to_other_province_but_not_overseas(self):
        self.schedule.sell("S1003", "外省", 2)  # 中国大陆授权
        with self.assertRaises(SchedulingError) as ctx:
            self.schedule.sell("S1003", "境外", 1)
        self.assertIn("INV-03", codes(ctx.exception.violations))

    def test_province_only_title_blocks_other_province(self):
        self.schedule.publish("S1006")
        self.schedule.sell("S1006", "本省其他", 10)  # 本省授权覆盖
        with self.assertRaises(SchedulingError) as ctx:
            self.schedule.sell("S1006", "外省", 1)
        self.assertIn("INV-03", codes(ctx.exception.violations))


class ChangeBlastRadiusTest(unittest.TestCase):
    def setUp(self):
        self.schedule = Schedule.from_fixture(FIXTURE)

    def test_guest_reschedule_only_touches_link(self):
        s1001_before = self.schedule._screening("S1001").copy()
        rights_before = self.schedule.rights_usage("G-F001-LINE")
        self.schedule.detach_guest("S1001", "G001")
        s1001 = self.schedule._screening("S1001")
        self.assertNotIn("G001", s1001["guest_ids"])
        # 排期时间、场地、授权、名额纹丝不动
        for key in ("start", "end", "venue", "free_seats", "sales_by_region", "status"):
            self.assertEqual(s1001[key], s1001_before[key])
        self.assertEqual(self.schedule.rights_usage("G-F001-LINE"), rights_before)
        # 想把嘉宾绑到她不可用的日期：整体回滚
        with self.assertRaises(SchedulingError) as ctx:
            self.schedule.attach_guest("S1002", "G001")  # 10-13 不在可到窗口
        self.assertIn("INV-09", codes(ctx.exception.violations))
        self.assertNotIn("G001", self.schedule._screening("S1002")["guest_ids"])

    def test_outdoor_cancel_releases_rights_venue_and_refunds_but_leaves_others_untouched(self):
        s1003_before = self.schedule._screening("S1003").copy()
        result = self.schedule.cancel("S1002", reason="雷雨预警")
        s1002 = self.schedule._screening("S1002")
        self.assertEqual(s1002["status"], "已取消")
        self.assertEqual(result["refunds"], {"本市": 60})           # 自动全额退票
        self.assertEqual(result["released_grant"], "G-F001-OUT")
        self.assertEqual(self.schedule.rights_usage("G-F001-OUT")["locked"], 0)
        self.assertEqual(s1002["sales_by_region"], {})
        # 其他渠道、其他影片场次完全不变
        s1003 = self.schedule._screening("S1003")
        for key in ("status", "sales_by_region", "venue", "start"):
            self.assertEqual(s1003[key], s1003_before[key])
        # 释放后，同一户外授权可以重排一场新的
        self.schedule.confirm({
            "id": "S2201", "film": "F001", "master": "M-F001-PRORES",
            "venue": "V-RIVERSIDE", "start": "2026-10-19T19:30", "end": "2026-10-19T21:30",
            "region": "本市", "required_subtitles": ["中文"],
        })
        self.assertEqual(self.schedule.rights_usage("G-F001-OUT")["locked"], 1)

    def test_change_hall_rolls_back_when_incompatible_or_too_small(self):
        original = self.schedule._screening("S1001")["venue"]
        # 阶梯教室容量 120，容不下 惠民20+已售120=140
        with self.assertRaises(SchedulingError):
            self.schedule.change_hall("S1001", "V-ROOM-B")
        self.assertEqual(self.schedule._screening("S1001")["venue"], original)
        # 换到一号厅成功：设备兼容、容量更大，授权次数不变
        usage_before = self.schedule.rights_usage("G-F001-LINE")
        self.schedule.change_hall("S1001", "V-CINEMA1")
        self.assertEqual(self.schedule._screening("S1001")["venue"], "V-CINEMA1")
        self.assertEqual(self.schedule.rights_usage("G-F001-LINE"), usage_before)

    def test_reschedule_inside_window_works_outside_gets_rejected(self):
        original = ("2026-10-12T19:00", "2026-10-12T21:00")
        # G001 的另一可到窗口是 10-18 晚，且授权窗口覆盖
        self.schedule.reschedule("S1001", "2026-10-18T19:00", "2026-10-18T21:00")
        self.assertEqual(
            (self.schedule._screening("S1001")["start"], self.schedule._screening("S1001")["end"]),
            ("2026-10-18T19:00", "2026-10-18T21:00"),
        )
        with self.assertRaises(SchedulingError):
            self.schedule.reschedule("S1001", "2026-11-11T19:00", "2026-11-11T21:00")
        self.assertEqual(
            (self.schedule._screening("S1001")["start"], self.schedule._screening("S1001")["end"]),
            ("2026-10-18T19:00", "2026-10-18T21:00"),
        )
        self.schedule.reschedule("S1001", *original)

    def test_refund_releases_seats_but_never_rights(self):
        usage_before = self.schedule.rights_usage("G-F001-LINE")
        self.schedule.refund("S1001", "本市", 50)
        self.assertEqual(self.schedule._screening("S1001")["sales_by_region"]["本市"], 70)
        self.assertEqual(self.schedule.rights_usage("G-F001-LINE"), usage_before)


class TechnicalIssueFlowTest(unittest.TestCase):
    def test_blocking_issue_pauses_publish_until_resolved(self):
        schedule = Schedule.from_fixture(FIXTURE)
        issue_id = schedule.report_issue("S1005", "密钥尚未下发", blocking=True)
        self.assertEqual(schedule._screening("S1005")["status"], "需调整")
        with self.assertRaises(SchedulingError):
            schedule.publish("S1005")
        schedule.resolve_issue(issue_id)
        self.assertEqual(schedule._screening("S1005")["status"], "已排片")
        schedule.publish("S1005")
        self.assertEqual(schedule._screening("S1005")["status"], "可开放")


class CloseOutTest(unittest.TestCase):
    def setUp(self):
        self.schedule = Schedule.from_fixture(FIXTURE)
        self.schedule.publish("S1005")

    def test_close_out_locks_consumption_and_receipt_is_reconcilable(self):
        receipt = self.schedule.close_out("S1005", "M-F005-DCP", attendees=300)
        self.assertEqual(receipt["reported_to"], "修复影像基金会")
        self.assertEqual(receipt["rights"], [{"license": "L-F005", "grant": "G-F005-CIN", "screenings_consumed": 1}])
        self.assertEqual(self.schedule._screening("S1005")["status"], "已核销")
        usage = self.schedule.rights_usage("G-F005-CIN")
        self.assertEqual((usage["locked"], usage["consumed"]), (0, 1))
        rows = self.schedule.reconcile("修复影像基金会")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["实际母版"], "M-F005-DCP")
        self.assertFalse(rows[0]["是否替换"])
        self.assertEqual(rows[0]["观众数"], 300)

    def test_over_capacity_and_unknown_master_rejected(self):
        with self.assertRaises(SchedulingError) as ctx:
            self.schedule.close_out("S1005", "M-F005-DCP", attendees=400)
        self.assertIn("INV-14", codes(ctx.exception.violations))
        with self.assertRaises(SchedulingError) as ctx:
            self.schedule.close_out("S1005", "M-F001-DCP", attendees=100)
        self.assertIn("INV-14", codes(ctx.exception.violations))
        # 被拒后场次仍可正常核销
        self.schedule.close_out("S1005", "M-F005-DCP", attendees=300)

    def test_substituted_master_is_flagged_for_holder(self):
        """实际换用另一母版时，回执明确标注替换，供版权方逐场核对。"""
        receipt = self.schedule.close_out("S1005", "M-F005-DCP", attendees=280)
        self.assertFalse(receipt["substituted"])
        rows = [r for r in self.schedule.reconcile("修复影像基金会") if r["场次"] == "S1005"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["容量"], 320)
        self.assertLessEqual(rows[0]["观众数"], rows[0]["容量"])


if __name__ == "__main__":
    unittest.main()
