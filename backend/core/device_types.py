# backend/core/device_types.py
# 设备类型登记表：care 侧的「设备谱系契约」。
#
# 为什么需要它（改造前的问题）：
#   告警研判原来对任何设备都用同一套通用读数，而且库里查不到数据时**现场编造模拟读数**
#   （心率 135、离床 15 分钟…）——研判结论因此由硬编码常量决定，而不是现场证据。
#
# 这张表解决三件事：
#   1. 按设备类型决定采集什么指标（床垫给心率/呼吸/离床，跌倒雷达给跌倒事件）；
#   2. 把「证据可信度」带进研判：贴身压力传感和红外测温的误报率不是一个量级，
#      四条轨道不能等权，来源本身要参与加权；
#   3. 类型编码对齐机构业务系统的设备类型字典，
#      业务系统下发的报警记录（带「设备类型」）能直接落到 care 的研判链路。
#
# 编码来源：机构业务系统已有的 9 类养老设备（床垫/床帘/手环/雷达/闸机/人脸考勤/
#          巡更器/人流测温/一体机）。业务系统只提供设备类型字典，
#          **指标与告警类型的映射是 care 侧定义的契约**——
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
# 设备谱系（对齐 SaaS 的 9 类设备）
# ──────────────────────────────────────────────────────────────

DEVICE_TYPES: dict[str, DeviceType] = {
    "mattress": DeviceType(
        code="mattress", name="智能床垫",
        metrics=("心率", "呼吸", "离床", "在床状态"),
        alert_types=("heart_rate_abnormal", "breath_abnormal", "off_bed_timeout"),
        trust_level=4,
        trust_reason="贴身压力与毫米波传感，直接测生理信号；但翻身、体位变化会造成瞬时波动",
        aliases=("智能床垫", "床垫", "离床传感器"),
    ),
    "radar": DeviceType(
        code="radar", name="跌倒雷达",
        metrics=("跌倒事件", "人体存在", "活动量"),
        alert_types=("fall_detected", "activity_low"),
        trust_level=3,
        trust_reason="非接触毫米波，跌倒事件判定受遮挡与多人环境影响，需结合现场确认",
        aliases=("跌倒雷达", "跌倒检测", "雷达"),
    ),
    "bracelet": DeviceType(
        code="bracelet", name="智能手环",
        metrics=("心率", "血氧", "定位", "SOS"),
        alert_types=("heart_rate_abnormal", "sos_pressed", "off_bed_timeout"),
        trust_level=3,
        trust_reason="腕部光电心率易受佩戴松动、出汗影响；SOS 为老人主动触发，优先级高",
        aliases=("手环", "智能手环", "心率手环"),
    ),
    "dx_health": DeviceType(
        code="dx_health", name="健康一体机",
        metrics=("血压", "心率", "体温", "血氧"),
        alert_types=("vital_abnormal", "heart_rate_abnormal"),
        trust_level=4,
        trust_reason="定点测量、有测量动作与时间点，数据质量接近临床护理测量",
        aliases=("一体机", "健康一体机", "血氧仪"),
    ),
    "gate": DeviceType(
        code="gate", name="门岗闸机",
        metrics=("出入记录", "通行方向"),
        alert_types=("wander_detected",),
        trust_level=4,
        trust_reason="事件型记录、无生理噪声；但只说明「该人经过」，不说明其状态",
        aliases=("门岗", "闸机", "gate"),
    ),
    "face_clock": DeviceType(
        code="face_clock", name="人脸考勤",
        metrics=("打卡记录",),
        alert_types=(),
        trust_level=5,
        trust_reason="人工动作留痕，作为「谁在岗、何时到」的证据几乎无争议",
        aliases=("人脸考勤", "打卡机"),
    ),
    "dx_patrol": DeviceType(
        code="dx_patrol", name="巡更器",
        metrics=("巡检打卡", "巡点记录"),
        alert_types=(),
        trust_level=5,
        trust_reason="巡点打卡为人工动作留痕，是「巡视是否真的发生过」的直接证据",
        aliases=("巡更", "巡更器", "巡检"),
    ),
    "flow_people": DeviceType(
        code="flow_people", name="人流测温",
        metrics=("人流量", "体表温度", "环境温度", "环境湿度"),
        alert_types=("room_temp_abnormal", "crowd_density"),
        trust_level=2,
        trust_reason="红外测温受环境温度与距离影响大，个体体温数值不宜作为单人健康判据",
        aliases=("人流测温", "人流", "温湿度传感器"),
    ),
    "bed_hammock": DeviceType(
        code="bed_hammock", name="智能床帘",
        metrics=("开合状态",),
        alert_types=(),
        trust_level=3,
        trust_reason="机械状态量，几乎不误报；但语义弱，只能作为隐私场景的旁证",
        aliases=("床帘", "智能床帘", "床帘机"),
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
