# tests/test_alert_actions.py
# 处置措施非空保障：LLM 漏字段时不能让工单变成"措施：[]"。
#
# 背景（真实观察到的问题）：风险评估的结构化输出 schema 里 recommended_actions
# 带默认空列表，模型偶尔省略该字段时校验照样通过，于是一路传到派单节点：
# 工单文本生成了、措施列表却是空的，值班主管批准后拿到一张没有可执行动作的工单。

from backend.agents.alert.nodes import (
    DEFAULT_ACTIONS_BY_RISK,
    assess_risk_node,
    ensure_actions,
)
from backend.agents.alert.state import RiskAssessment


def test_ensure_actions_backfills_by_risk_level():
    """空措施按风险等级补保底动作。"""
    for level in ("high", "medium", "low"):
        actions = ensure_actions(level, [])
        assert actions, f"{level} 风险应补出非空措施"
        assert actions == DEFAULT_ACTIONS_BY_RISK[level]
    assert ensure_actions("unknown", []), "未知等级也要有兜底"


def test_ensure_actions_keeps_model_output():
    """模型给了具体措施就原样保留，不改写、不叠加通用动作。"""
    given = ["给点位吸氧", "测量血压"]
    assert ensure_actions("high", given) == given


def test_ensure_actions_cleans_blank_items():
    """只有空白项等同为空，需要补兜底。"""
    assert ensure_actions("medium", ["", "   "]) == DEFAULT_ACTIONS_BY_RISK["medium"]
    assert ensure_actions("medium", [" 测量体温 ", ""]) == ["测量体温"]


class _FakeStructuredLLM:
    """模拟结构化输出：模型返回空措施（字段有默认值，校验不会报错）。"""

    def __init__(self, risk: RiskAssessment):
        self._risk = risk

    async def ainvoke(self, _messages):
        return self._risk


async def test_assess_risk_node_backfills_empty_actions(monkeypatch):
    """节点层兜底：模型漏措施时，落进 State 的风险评估必须带非空措施。"""
    empty = RiskAssessment(
        risk_level="high", risk_score=85, reasoning="电流持续过载且柜内温升偏高",
        recommended_actions=[], confidence=0.8,
    )
    monkeypatch.setattr(
        "backend.agents.alert.nodes.get_structured_llm",
        lambda *_a, **_k: _FakeStructuredLLM(empty),
    )

    result = await assess_risk_node({
        "alert_type": "heart_rate_abnormal",
        "alert_timestamp": "2026-09-29 20:00:00",
        "device_readings": [], "service_records": [], "health_indicators": [],
    })

    risk = result["risk_assessment"]
    assert risk["recommended_actions"], "空措施没有被兜底"
    assert risk["risk_level"] == "high"
    assert risk["recommended_actions"] == DEFAULT_ACTIONS_BY_RISK["high"]


async def test_assess_risk_node_keeps_model_actions(monkeypatch):
    """模型给了措施就不动它（兜底不能覆盖模型的具体判断）。"""
    given = ["立即测量指尖血糖", "通知使用方"]
    risk = RiskAssessment(
        risk_level="medium", risk_score=60, reasoning="血糖偏高",
        recommended_actions=given, confidence=0.7,
    )
    monkeypatch.setattr(
        "backend.agents.alert.nodes.get_structured_llm",
        lambda *_a, **_k: _FakeStructuredLLM(risk),
    )

    result = await assess_risk_node({
        "alert_type": "glucose_abnormal", "alert_timestamp": "2026-09-29 20:00:00",
        "device_readings": [], "service_records": [], "health_indicators": [],
    })
    assert result["risk_assessment"]["recommended_actions"] == given
