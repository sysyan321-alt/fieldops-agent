# tests/test_alert_tracks_and_risk_gate.py
# 两块"对外承诺了、必须有代码兜住"的行为：
#   ① SOP 快通道的高危门控——高危问法必须强制走慢链路；
#   ② 告警四轨采集——第四轨是老人最近一次能力评估，缺记录不算失败。

from backend.agents.alert import nodes as alert_nodes
from backend.agents.sop_qa.nodes import quick_route_node
from backend.core.quick_index import HIGH_RISK_KEYWORDS, get_quick_index, is_high_risk


# ── ① 高危门控 ────────────────────────────────────────────────

def test_is_high_risk_detects_emergencies():
    for q in ["老人突然窒息怎么办", "噎住了怎么急救", "老人意识不清叫不醒",
              "心跳骤停怎么做心肺复苏", "伤口大出血止不住"]:
        assert is_high_risk(q), f"「{q}」应判为高危"


def test_is_high_risk_ignores_routine_questions():
    """常规护理提问不能被误拦（否则快通道直接失效）。"""
    for q in ["压疮分几期", "多久给老人翻一次身", "喂鼻饲前要回抽吗", "防压疮用什么评估表"]:
        assert not is_high_risk(q), f"「{q}」不该判为高危"


async def test_high_risk_query_is_forced_to_slow_path():
    """高危问法即使能命中本地索引，也必须回落慢链路。"""
    index = get_quick_index()
    # 特意选一条"本来会命中索引"的高危问法：没门控时它会拿到一节 SOP 原文，
    # 有门控则必须回落慢链路（这条能让门控真的被验证，而不是碰巧没命中）。
    query = "口腔护理时出血不止怎么办"
    normal_hit = index.search(query).get("hit")

    state = {"original_query": query, "tenant_id": "tenant_default"}
    result = await quick_route_node(state)

    assert result["answer_mode"] == "rag", "高危问法没有回落慢链路"
    assert result.get("high_risk") is True
    # 说明门控确实起了作用：本地索引其实是有命中的
    assert normal_hit is not None, "这条用例失去意义：索引本来就没命中，请换一条问法"


async def test_routine_query_still_uses_fast_channel():
    """门控不能把正常问法也拦掉。"""
    result = await quick_route_node({"original_query": "压疮分几期", "tenant_id": "tenant_default"})
    assert result["answer_mode"] == "quick"
    assert result.get("quick_hit")


def test_keyword_list_is_not_oversized():
    """关键词表要克制：越宽越容易把常规提问误判成应急。"""
    assert len(HIGH_RISK_KEYWORDS) <= 30


# ── ② 四轨采集 ────────────────────────────────────────────────

def test_format_assessment_snapshot_handles_missing():
    assert alert_nodes._format_assessment_snapshot(None) == "（暂无评估记录）"


def test_format_assessment_snapshot_renders_key_fields():
    text = alert_nodes._format_assessment_snapshot({
        "created_at": "2026-09-20 10:00:00",
        "weighted_score": 62.5,
        "dimension_scores": [{"dimension": "ADL", "score": 55}, {"dimension": "认知", "score": 70}],
        "summary": {
            "care_level_recommendation": "介助",
            "issues": [{"suggestion": "夜间加装床栏"}],
        },
    })
    assert "2026-09-20" in text
    assert "62.5" in text
    assert "ADL 55" in text
    assert "介助" in text
    assert "夜间加装床栏" in text


async def test_four_tracks_gather_returns_all_four(monkeypatch):
    """四轨并行：任一轨失败不阻塞其他轨，缺评估记录也不报错。"""
    async def _devices(_state):
        return [{"device_type": "心率手环", "reading_value": "135"}]

    async def _care(_state):
        return [{"care_type": "巡视", "content": "夜间巡视正常"}]

    async def _health(_state):
        raise RuntimeError("健康指标服务异常")      # 第三轨失败

    async def _assessment(_state):
        raise RuntimeError("评估库超时")            # 第四轨失败

    monkeypatch.setattr(alert_nodes, "_fetch_device_data", _devices)
    monkeypatch.setattr(alert_nodes, "_fetch_care_records", _care)
    monkeypatch.setattr(alert_nodes, "_fetch_health_indicators", _health)
    monkeypatch.setattr(alert_nodes, "_fetch_assessment_snapshot", _assessment)

    result = await alert_nodes.run_four_tracks_node({"alert_type": "heart_rate_abnormal"})

    assert len(result["device_readings"]) == 1
    assert len(result["care_records"]) == 1
    assert result["health_indicators"] == [], "失败的那一轨应降级为空列表"
    assert result["assessment_snapshot"] is None, "缺评估记录应为 None，而不是抛异常"


def test_risk_prompt_accepts_fourth_track():
    """提示词必须能接受第四轨（缺占位符会直接 KeyError/格式化失败）。"""
    from backend.agents.alert.prompts import RISK_ASSESS_PROMPT

    text = RISK_ASSESS_PROMPT.format(
        alert_type="heart_rate_abnormal", alert_timestamp="2026-09-29 21:00:00",
        device_profile="设备类型：智能床垫（mattress），证据可信度 4/5",
        evidence_quality="partial", evidence_gaps="健康指标",
        device_readings="（无）", care_records="（无）", health_indicators="（无）",
        assessment_snapshot="（暂无评估记录）",
    )
    assert "第四轨" in text
    # 证据纪律必须落在提示词里，否则"缺证据"会被模型当成"没异常"
    assert "证据完整性" in text and "insufficient" in text
    assert "不得推测" in text
