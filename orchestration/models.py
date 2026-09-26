"""多渠道展映编排的领域记录。

记录类型与 fixtures/domain.json 的约定保持一致: 影片母版、放映授权、
场地设备、排片场次、座席名额、放映回执; 另补充映后嘉宾与适配问题,
用于映后活动排期和排期校验产出的传递。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Channel(str, Enum):
    """四类放映板块。"""

    CINEMA = "影院"
    ONLINE = "线上"
    OUTDOOR = "户外"
    CAMPUS = "校园"


# 线下板块: 影院、户外、校园; 部分版权仅授权线下场次。
OFFLINE_CHANNELS = frozenset({Channel.CINEMA, Channel.OUTDOOR, Channel.CAMPUS})


class SessionState(str, Enum):
    """排片场次的流程状态, 对应领域资料中的 workflow_states。"""

    PENDING_ADAPTATION = "待适配"
    SCHEDULED = "已排片"
    OPENABLE = "可开放"
    NEEDS_ADJUSTMENT = "需调整"
    SCREENING = "放映中"
    RECONCILED = "已核销"
    CANCELLED = "已取消"


# 分辨率能力从低到高排序, 用于母版与设备之间的适配判断。
RESOLUTION_RANK = {"720p": 1, "1080p": 2, "2K": 3, "4K": 4}


@dataclass
class FilmMaster:
    """影片母版: 版权方交付的放映素材及其技术规格。"""

    film_id: str
    title: str
    container: str                      # 封装格式, 如 "DCP" / "MP4" / "ProRes"
    video_codec: str                    # 视频编码, 如 "JPEG2000" / "H.265"
    audio_layout: str                   # 声道布局, 如 "5.1" / "2.0"
    resolution: str                     # 母版分辨率, 取 RESOLUTION_RANK 的键
    subtitle_languages: frozenset[str]  # 已有字幕轨的语言代码
    rating_notice: str | None           # 分级提示文案, None 表示尚未提供
    runtime_minutes: int


@dataclass
class ScreeningLicense:
    """放映授权: 版权方授予的渠道、地域、时段与次数限制。"""

    license_id: str
    film_id: str
    licensor: str                       # 版权方
    allowed_channels: frozenset[Channel]
    max_screenings: int                 # 授权放映总次数
    regions: frozenset[str] | None      # 授权地域; None 表示不限地域
    valid_from: datetime
    valid_until: datetime
    consumed: int = 0                   # 已锁住的授权次数

    @property
    def remaining(self) -> int:
        """尚未锁住的授权次数。"""
        return self.max_screenings - self.consumed


@dataclass
class VenueEquipment:
    """场地设备: 一个可排期的放映空间及其解码/放映能力。"""

    venue_id: str
    name: str
    channel: Channel
    region: str
    capacity: int                       # 座席数或线上并发名额
    containers: frozenset[str]          # 可播放的封装格式
    codecs: frozenset[str]              # 可解码的视频编码
    audio_layouts: frozenset[str]       # 可还音的声道布局
    max_resolution: str                 # 设备支持的最高分辨率


@dataclass
class Guest:
    """映后嘉宾及其可出席时段。"""

    guest_id: str
    name: str
    available_from: datetime
    available_until: datetime


@dataclass
class Session:
    """排片场次: 一部影片在一个场地的一次计划放映。"""

    session_id: str
    film_id: str
    license_id: str
    venue_id: str
    start: datetime
    end: datetime
    channel: Channel = Channel.CINEMA   # 登记排期时会按场地改写
    state: SessionState = SessionState.PENDING_ADAPTATION
    region: str | None = None           # 线上场次的目标地域, 线下场次取场地地域
    required_subtitles: frozenset[str] = frozenset()
    guest_ids: tuple[str, ...] = ()
    benefit_seats: int = 0              # 惠民(免费)座席数
    license_locked: bool = False        # 是否已锁住一次授权


@dataclass
class SeatQuota:
    """座席名额: 对外发布的可售与免费名额, 合计恒等于场地容量。"""

    session_id: str
    capacity: int
    sellable: int                       # 可售名额(含已售)
    benefit: int                        # 惠民免费名额(含已领取)
    sold: int = 0
    benefit_claimed: int = 0


@dataclass
class ScreeningReceipt:
    """放映回执: 放映结束后供版权方逐场核对的实际数据。"""

    session_id: str
    film_id: str
    license_id: str
    played: bool
    actual_plays: int                   # 实际播放次数
    attendance: int                     # 实际观众数
    capacity: int                       # 场次观众容量
    rights_consumed: int                # 本场消耗的授权次数


@dataclass(frozen=True)
class Issue:
    """适配问题: 排期校验的产出, 提前交给技术人员处理。"""

    code: str
    message: str
    subject_id: str                     # 相关场次
    blocker: bool = True                # 阻断确认排期的问题
