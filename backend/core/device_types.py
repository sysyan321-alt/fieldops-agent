# backend/core/device_types.py
# 设备类型登记表：care 侧的「设备谱系契约」。
#
# 为什么需要它（改造前的问题）：
#   告警研判原来对任何设备都用同一套通用读数，而且库里查不到数据时**现场编造模拟读数**
#   （心率 135、离床 15 分钟…）——研判结论因此由硬编码常量决定，而不是现场证据。
#
# 这张表解决三件事：
#   1. 按设备类型决定采集什么指标（配电监测给电流/电压/温度，视频分析给跌倒事件）；
#   2. 把「证据可信度」带进研判：直接测电气参数的仪表和人流量计数器的误报率
#      不是一个量级，四条轨道不能等权，来源本身要参与加权；
#   3. 类型编码对齐机构业务系统的设备类型字典，
#      业务系统下发的报警记录（带「设备类型」）能直接落到本服务的研判链路。
#
# 编码来源：园区/物业场景常见的 9 类现场设备（配电监测/视频行为分析/便携巡检终端/
#          环境监测站/门禁/人员定位卡/巡更器/人流量统计/水位监测）。
#          业务系统只提供设备类型字典，
#          **指标与告警类型的映射是本服务侧定义的契约**——
#          它描述"这类设备能给出什么证据"，属于研判逻辑的一部分。

from __future__ import annotations

from dataclasses import dataclass

from backend.core.logger import get_logger

logger = get_logger(__name__)

# 证据可信度取值：1（最弱，易误报）~ 5（最强，人工动作留痕）
TRUST_MIN, TRUST_MAX = 1, 5


@dataclass(frozen=True)
class DeviceType:
    """一类设备：能上报什么指标、能产生什么告警、证据有多可信。"""

    code: str
    name: str
    metrics: tuple[str, ...]      # 该设备能上报的指标名
    alert_types: tuple[str, ...]  # 该设备可能产生的告警类型
    trust_level: int              # 证据可信度 1-5
    trust_reason: str             # 为什么是这个可信度（写进研判 prompt）
    aliases: tuple[str, ...] = () # 兼容历史自由字符串（如早期种子数据的"心率手环"）


# ──────────────────────────────────────────────────────────────
# 设备谱系（园区/物业现场常见的 9 类设备）
#
# 每类登记三件事，研判侧据此决定"采什么证据"与"这份证据能信到什么程度"：
#   metrics      —— 这类设备能上报哪些指标
#   alert_types  —— 它可能产生哪些告警（反向用于按告警类型兜底推断设备）
#   trust_level  —— 证据可信度 1-5，以及为什么（会原样写进研判提示词）
# ──────────────────────────────────────────────────────────────

DEVICE_TYPES: dict[str, DeviceType] = {
    "power_monitor": DeviceType(
        code="power_monitor", name="配电监测",
        metrics=("电流", "电压", "设备温度", "功率"),
        alert_types=("overcurrent", "device_offline"),
        trust_level=4,
        trust_reason="直接测电气参数、无介质干扰；但大功率设备启动的短时电流会造成瞬时越限",
        aliases=("配电监测", "配电柜", "电力监测"),
    ),
    "video_analytics": DeviceType(
        code="video_analytics", name="视频行为分析",
        metrics=("跌倒事件", "人员闯入", "区域人数"),
        alert_types=("fall_detected", "intrusion_detected"),
        trust_level=3,
        trust_reason="视觉判定受遮挡、逆光与多人环境影响，需结合现场画面确认",
        aliases=("视频行为分析", "行为分析", "摄像头", "视频分析"),
    ),
    "patrol_terminal": DeviceType(
        code="patrol_terminal", name="便携巡检终端",
        metrics=("巡检打卡", "人员定位", "SOS"),
        alert_types=("sos_pressed", "missed_patrol"),
        trust_level=3,
        trust_reason="打卡为人工动作、可信；但 SOS 依赖人员主动触发，未必覆盖已失去行动能力的情形",
        aliases=("便携巡检终端", "巡检终端", "手持机", "对讲终端"),
    ),
    "env_station": DeviceType(
        code="env_station", name="环境监测站",
        metrics=("温度", "湿度", "可燃气", "有毒气体"),
        alert_types=("env_temp_abnormal", "air_quality_abnormal"),
        trust_level=4,
        trust_reason="定点安装、有定期标定，数据质量接近仪表读数；但只代表探头所在位置",
        aliases=("环境监测站", "环境监测", "气体探测器", "温湿度传感器"),
    ),
    "access_control": DeviceType(
        code="access_control", name="门禁",
        metrics=("通行记录", "开门方式", "强开状态"),
        alert_types=("gate_forced", "intrusion_detected"),
        trust_level=4,
        trust_reason="事件型记录、无介质噪声；但只说明「门被打开」，不说明进入者身份与意图",
        aliases=("门禁", "门禁系统", "闸机", "门岗"),
    ),
    "locator_tag": DeviceType(
        code="locator_tag", name="人员定位卡",
        metrics=("打卡记录", "区域定位"),
        alert_types=(),
        trust_level=5,
        trust_reason="人工动作留痕，作为「谁在岗、何时到位」的证据几乎无争议",
        aliases=("人员定位卡", "定位卡", "考勤卡", "人脸考勤"),
    ),
    "patrol_reader": DeviceType(
        code="patrol_reader", name="巡更器",
        metrics=("巡检打卡", "巡点记录"),
        alert_types=("missed_patrol",),
        trust_level=5,
        trust_reason="巡点打卡为人工动作留痕，是「巡检是否真的发生过」的直接证据",
        aliases=("巡更器", "巡更", "巡检点", "巡更棒"),
    ),
    "people_counter": DeviceType(
        code="people_counter", name="人流量统计",
        metrics=("人流量", "进出方向"),
        alert_types=("crowd_density",),
        trust_level=2,
        trust_reason="视觉/红外计数在逆光、遮挡、并行通过时误差大，单源不足以定性",
        aliases=("人流量统计", "人流统计", "客流统计"),
    ),
    "water_level": DeviceType(
        code="water_level", name="水位监测",
        metrics=("水位", "渗漏状态"),
        alert_types=("water_level_abnormal",),
        trust_level=3,
        trust_reason="状态量、误报少；但安装位置决定它只能覆盖本地局部点位",
        aliases=("水位监测", "水浸传感器", "液位计", "渗漏监测"),
    ),
}

# 名称/别名 → 类型编码（大小写与中英文都要能命中）
_ALIAS_INDEX: dict[str, str] = {}
for _t in DEVICE_TYPES.values():
    _ALIAS_INDEX[_t.code.lower()] = _t.code
    _ALIAS_INDEX[_t.name] = _t.code
    for _a in _t.aliases:
        _ALIAS_INDEX[_a] = _t.code

# 告警类型 → 该告警**应当**由哪类设备产生（用于校验/兜底推断）
_ALERT_TO_TYPES: dict[str, tuple[str, ...]] = {}
for _t in DEVICE_TYPES.values():
    for _at in _t.alert_types:
        _ALERT_TO_TYPES[_at] = _ALERT_TO_TYPES.get(_at, ()) + (_t.code,)


# ──────────────────────────────────────────────────────────────
# 查询接口
# ──────────────────────────────────────────────────────────────

def resolve_device_type(code_or_name: str | None) -> DeviceType | None:
    """把设备类型编码或名称（含历史自由字符串）解析成登记项；认不出返回 None。

    认不出时**不抛异常也不猜**：调用方按"未知类型的通用设备"处理，
    并在证据里标注不可信——这正是原来那套硬编码模拟数据要避免的行为。
    """
    if not code_or_name:
        return None
    key = str(code_or_name).strip()
    if key in DEVICE_TYPES:
        return DEVICE_TYPES[key]
    hit = _ALIAS_INDEX.get(key) or _ALIAS_INDEX.get(key.lower())
    return DEVICE_TYPES.get(hit) if hit else None


def trust_level_of(code_or_name: str | None) -> int:
    """证据可信度；未知设备取中间偏下值（不假设它可信）。"""
    dt = resolve_device_type(code_or_name)
    return dt.trust_level if dt else 3


def device_types_for_alert(alert_type: str | None) -> tuple[str, ...]:
    """反查：某类告警通常来自哪些设备（SaaS 没带设备类型时用来兜底推断）。"""
    return _ALERT_TO_TYPES.get(str(alert_type or ""), ())


def evidence_hint(code_or_name: str | None) -> str:
    """把设备类型压成一行给研判 prompt 用（未知类型明确标注）。"""
    dt = resolve_device_type(code_or_name)
    if dt is None:
        return (f"设备类型：{code_or_name or '未提供'}（未登记，证据可信度按 3 处理，"
                "不得仅凭该设备结论定性）")
    return (f"设备类型：{dt.name}（{dt.code}），证据可信度 {dt.trust_level}/5 —— "
            f"{dt.trust_reason}")


def metrics_of(code_or_name: str | None) -> tuple[str, ...]:
    """该设备能上报的指标；未知设备返回空元组（→ 调用方按通用采集处理）。"""
    dt = resolve_device_type(code_or_name)
    return dt.metrics if dt else ()
