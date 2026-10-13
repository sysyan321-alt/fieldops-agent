# tests/test_work_flow_stage_mode.py
# 照护流程：异常子模式切换与阶段流转单元测试
# 不依赖真实 LLM / DB——只验证 check_stage_node 的规则逻辑。

import pytest
from langchain_core.messages import HumanMessage

from backend.agents.work_flow.nodes import (
    check_stage_node,
    ABNORMAL_KEYWORDS,
    ABNORMAL_DONE_KEYWORDS,
    ABNORMAL_MAX_TURNS,
    STAGE_MIN_TURNS,
    STAGE_MAX_TURNS,
)
from backend.agents.work_flow.state import WorkStage, DailyMode


def _state(stage="daily_inspection", stage_turns=5, total_turns=10,
           mode=DailyMode.NORMAL.value, abnormal_turns=0,
           last_input="晨间护理完成", discharge_order=None,
           max_turns=40):
    """构造一份只含必要字段的 State。"""
    state: dict = {
        "current_stage": stage,
        "stage_turn_count": stage_turns,
        "total_turn_count": total_turns,
        "max_turns": max_turns,
        "daily_mode": mode,
        "abnormal_turn_count": abnormal_turns,
        "messages": [HumanMessage(content=last_input)],
    }
    # 仅在测试显式传值时塞 discharge_order，None 等价于「续住」
    if discharge_order is not None:
        state["discharge_order"] = discharge_order
    return state


# ── 层1：子模式切换 ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_normal_to_abnormal_by_keyword():
    """日常巡检 normal → abnormal：命中关键词即切模式，不切阶段。"""
    s = _state(mode="normal", last_input="晨护时发现压红，皮肤有破损")
    out = await check_stage_node(s)
    assert out["daily_mode"] == "abnormal"
    assert out["abnormal_turn_count"] == 0
    # 不切阶段
    assert "current_stage" not in out


@pytest.mark.asyncio
async def test_abnormal_to_normal_by_done_keyword():
    """abnormal → normal：命中'异常处置完'等关键词。"""
    s = _state(mode="abnormal", abnormal_turns=3, last_input="异常处置完毕")
    out = await check_stage_node(s)
    assert out["daily_mode"] == "normal"
    assert out["abnormal_turn_count"] == 0


@pytest.mark.asyncio
async def test_abnormal_to_normal_by_max_turns():
    """abnormal → normal：异常轮数到上限自动切回。"""
    s = _state(mode="abnormal", abnormal_turns=ABNORMAL_MAX_TURNS,
               last_input="还在处理")
    out = await check_stage_node(s)
    assert out["daily_mode"] == "normal"


@pytest.mark.asyncio
async def test_abnormal_not_ended_without_keyword_or_max():
    """abnormal 既没命中'处理完'关键词、又没到 max → 保持在 abnormal。"""
    s = _state(mode="abnormal", abnormal_turns=1, last_input="伤口还在渗血")
    out = await check_stage_node(s)
    assert out == {}  # 无变化


@pytest.mark.asyncio
async def test_normal_no_keyword_no_switch():
    """normal 模式没命中异常关键词 → 不切模式。"""
    s = _state(mode="normal", last_input="继续翻身")
    out = await check_stage_node(s)
    assert out == {}


# ── 层2：异常模式下不让主动结束阶段 ──────────────────────────

@pytest.mark.asyncio
async def test_abnormal_mode_blocks_user_finish():
    """异常模式内，用户说'结束本阶段'也不生效——必须先回 normal。"""
    s = _state(mode="abnormal", abnormal_turns=1,
               stage_turns=STAGE_MAX_TURNS["daily_inspection"] - 1,
               last_input="结束本阶段")
    out = await check_stage_node(s)
    # 阶段不流转
    assert "current_stage" not in out


# ── 层2：正常阶段流转（原逻辑回归） ───────────────────────────

@pytest.mark.asyncio
async def test_daily_inspection_finish_to_review():
    """日常巡检 normal 模式 + 达到 min + 用户主动结束 → 流转阶段复评。"""
    s = _state(stage="daily_inspection", mode="normal", stage_turns=8,
               last_input="结束照护")
    out = await check_stage_node(s)
    assert out["current_stage"] == WorkStage.PERIODIC_REVIEW.value
    assert out["stage_turn_count"] == 0
    assert out["daily_mode"] == "normal"  # 阶段流转后默认重置 normal


@pytest.mark.asyncio
async def test_admission_reached_max_to_daily_inspection():
    """建档评估到 max → 自动流转到日常巡检。"""
    s = _state(stage="onboarding_assessment",
               stage_turns=STAGE_MAX_TURNS["onboarding_assessment"],
               mode="normal", last_input="继续")
    out = await check_stage_node(s)
    assert out["current_stage"] == WorkStage.DAILY_INSPECTION.value
    assert out["daily_mode"] == "normal"


@pytest.mark.asyncio
async def test_finished_returns_empty():
    """FINISHED 状态下无变化。"""
    s = _state(stage="finished", last_input="")
    out = await check_stage_node(s)
    assert out == {}


# ── 层2：PERIODIC_REVIEW 由 discharge_order 决定出院 / 续住循环 ────

@pytest.mark.asyncio
async def test_periodic_review_with_discharge_order_to_decommission_assessment():
    """阶段复评 → 退场评估：SaaS 下发了退住工单就走退场评估。"""
    s = _state(
        stage="periodic_review",
        stage_turns=STAGE_MAX_TURNS["periodic_review"],
        mode="normal",
        last_input="继续",
        discharge_order={"order_type": "discharge", "reason": "家属申请"},
    )
    out = await check_stage_node(s)
    assert out["current_stage"] == WorkStage.DECOMMISSION_ASSESSMENT.value
    assert out["stage_turn_count"] == 0
    assert out["daily_mode"] == "normal"


@pytest.mark.asyncio
async def test_periodic_review_without_discharge_order_loops_back_to_daily_inspection():
    """阶段复评 → 日常巡检：没有退住工单就回日常巡检、stage_turn_count 归零、开新循环。"""
    s = _state(
        stage="periodic_review",
        stage_turns=STAGE_MAX_TURNS["periodic_review"],
        mode="normal",
        last_input="结束本阶段",
        # 不传 discharge_order → 视为 None（续住）
    )
    out = await check_stage_node(s)
    assert out["current_stage"] == WorkStage.DAILY_INSPECTION.value
    assert out["stage_turn_count"] == 0
    assert out["daily_mode"] == "normal"  # 阶段流转后默认重置


@pytest.mark.asyncio
async def test_periodic_review_user_finish_keyword_also_routes_by_discharge_order():
    """阶段复评未到 max，但用户说'结束本阶段'：仍按 discharge_order 二选一。"""
    s_no_order = _state(
        stage="periodic_review",
        stage_turns=STAGE_MAX_TURNS["periodic_review"] - 1,  # 还差 1 轮到 max
        mode="normal",
        last_input="结束本阶段",
    )
    out_no_order = await check_stage_node(s_no_order)
    assert out_no_order["current_stage"] == WorkStage.DAILY_INSPECTION.value  # 续住循环

    s_with_order = _state(
        stage="periodic_review",
        stage_turns=STAGE_MAX_TURNS["periodic_review"] - 1,
        mode="normal",
        last_input="结束本阶段",
        discharge_order={"order_type": "discharge"},
    )
    out_with_order = await check_stage_node(s_with_order)
    assert out_with_order["current_stage"] == WorkStage.DECOMMISSION_ASSESSMENT.value  # 出院


@pytest.mark.asyncio
async def test_total_max_overrides_discharge_order_at_review():
    """总轮数到上限时，阶段复评的退住工单也要让位给 FINISHED（强制收尾生成报告）。

    设计取舍：max_turns 是硬帽——到顶必须停，不能继续走 DECOMMISSION_ASSESSMENT；
    退住工单可在下一会话里继续生效。
    """
    s = _state(
        stage="periodic_review",
        stage_turns=STAGE_MAX_TURNS["periodic_review"],
        mode="normal",
        last_input="继续",
        max_turns=10,
        discharge_order={"order_type": "discharge"},
    )
    out = await check_stage_node(s)
    assert out["current_stage"] == WorkStage.FINISHED.value


# ── 层2-补：discharge_order 边界与持久化的反向验证 ────────────────

@pytest.mark.asyncio
async def test_discharge_order_converges_all_stages_to_review_spine():
    """建档评估 / 日常巡检遇到工单 → 统一收敛到阶段复评（不跳过复评、也不停留）。

    防回归要点：非 PERIODIC_REVIEW 阶段不会"直接跳到退场评估"（跳过复评），
    而是并入 复评 → 退场评估 主干道；也不因工单无限停留在当前阶段。
    """
    # ① DAILY_INSPECTION + 工单 + 到达 max → PERIODIC_REVIEW
    s_daily = _state(
        stage="daily_inspection",
        stage_turns=STAGE_MAX_TURNS["daily_inspection"],
        mode="normal",
        last_input="结束照护",
        discharge_order={"order_type": "discharge"},
    )
    out_daily = await check_stage_node(s_daily)
    assert out_daily["current_stage"] == WorkStage.PERIODIC_REVIEW.value

    # ② ONBOARDING_ASSESSMENT + 工单 + 到达 max → 收敛到 PERIODIC_REVIEW（不再进日常巡检）
    s_adm = _state(
        stage="onboarding_assessment",
        stage_turns=STAGE_MAX_TURNS["onboarding_assessment"],
        mode="normal",
        last_input="继续",
        discharge_order={"order_type": "discharge"},
    )
    out_adm = await check_stage_node(s_adm)
    assert out_adm["current_stage"] == WorkStage.PERIODIC_REVIEW.value


# ── 层2-补：工单驱动的阶段收敛（收到工单 → 复评 → 退场评估 主干道） ──

@pytest.mark.asyncio
async def test_discharge_order_converges_daily_inspection_to_review_at_min():
    """日常巡检收到工单：达 min 即收敛到阶段复评，不必等满 max。"""
    s = _state(
        stage="daily_inspection",
        stage_turns=STAGE_MIN_TURNS["daily_inspection"],  # 刚到 min(8)，未到 max(15)
        mode="normal",
        last_input="继续",
        discharge_order={"order_type": "discharge", "reason": "转院"},
    )
    out = await check_stage_node(s)
    assert out["current_stage"] == WorkStage.PERIODIC_REVIEW.value
    assert out["stage_turn_count"] == 0
    assert out["daily_mode"] == "normal"
    assert out["abnormal_turn_count"] == 0


@pytest.mark.asyncio
async def test_discharge_converge_requires_min_turns():
    """日常巡检收到工单但未达 min → 本轮不收敛（保留最小照护轮数）。"""
    s = _state(
        stage="daily_inspection",
        stage_turns=STAGE_MIN_TURNS["daily_inspection"] - 1,  # 差 1 轮
        mode="normal",
        last_input="继续",
        discharge_order={"order_type": "discharge"},
    )
    out = await check_stage_node(s)
    assert out == {}  # 无流转


@pytest.mark.asyncio
async def test_discharge_order_converges_admission_to_review():
    """建档评估收到工单：走完基线(达 min)后直插阶段复评，跳过日常巡检。"""
    s = _state(
        stage="onboarding_assessment",
        stage_turns=STAGE_MIN_TURNS["onboarding_assessment"],  # 3
        mode="normal",
        last_input="继续",
        discharge_order={"order_type": "discharge", "reason": "病愈出院"},
    )
    out = await check_stage_node(s)
    assert out["current_stage"] == WorkStage.PERIODIC_REVIEW.value


@pytest.mark.asyncio
async def test_decommission_assessment_with_order_to_finished():
    """退场评估持有工单：达 min 即收敛到终态（触发报告生成）。"""
    s = _state(
        stage="decommission_assessment",
        stage_turns=STAGE_MIN_TURNS["decommission_assessment"],  # 2
        mode="normal",
        last_input="继续",
        discharge_order={"order_type": "discharge"},
    )
    out = await check_stage_node(s)
    assert out["current_stage"] == WorkStage.FINISHED.value


@pytest.mark.asyncio
async def test_abnormal_mode_blocks_discharge_convergence():
    """异常子模式内收到工单 + 达 min → 不收敛（异常先处理完），与"用户主动结束"同等对待。"""
    s = _state(
        stage="daily_inspection",
        stage_turns=STAGE_MIN_TURNS["daily_inspection"],
        mode="abnormal",
        abnormal_turns=1,
        last_input="伤口还在渗血",
        discharge_order={"order_type": "discharge"},
    )
    out = await check_stage_node(s)
    assert "current_stage" not in out  # 不切阶段


@pytest.mark.asyncio
async def test_discharge_converge_still_bounded_by_total_max():
    """工单收敛仍受会话硬帽约束：总轮数到顶 → FINISHED（而不是走复评重开循环）。"""
    s = _state(
        stage="daily_inspection",
        stage_turns=STAGE_MIN_TURNS["daily_inspection"],
        total_turns=10,
        max_turns=10,
        mode="normal",
        last_input="继续",
        discharge_order={"order_type": "discharge"},
    )
    out = await check_stage_node(s)
    assert out["current_stage"] == WorkStage.FINISHED.value


@pytest.mark.asyncio
async def test_daily_inspection_without_order_before_max_stays():
    """反向验证：日常巡检无工单、未到 max、用户不主动结束 → 不流转（原逻辑不被破坏）。"""
    s = _state(
        stage="daily_inspection",
        stage_turns=STAGE_MIN_TURNS["daily_inspection"],
        mode="normal",
        last_input="继续翻身",
    )
    out = await check_stage_node(s)
    assert out == {}


@pytest.mark.asyncio
async def test_empty_dict_discharge_order_treated_as_none():
    """空 dict `{}` 与 None 等价（falsy）：阶段复评也走续住循环。

    前端可能传 `{}` 占位（表单字段未填），等价于不传；不能用 dict 是否为 None 判断。
    """
    s = _state(
        stage="periodic_review",
        stage_turns=STAGE_MAX_TURNS["periodic_review"],
        mode="normal",
        last_input="继续",
        discharge_order={},  # 空 dict 显式 falsy
    )
    out = await check_stage_node(s)
    assert out["current_stage"] == WorkStage.DAILY_INSPECTION.value  # 续住循环
    assert "discharge_order" not in out


@pytest.mark.asyncio
async def test_loop_back_resets_substate_counters_too():
    """续住循环时 abnormal_turn_count / daily_mode 也一并归零，新周期从干净状态起步。

    避免上一轮的异常子模式计数污染下一周期——例如上一周期积累了 5 轮 abnormal，
    不归零会导致下一周期一进门就被判定为 ABNORMAL_MAX_TURNS、强制切回 normal。
    """
    s = _state(
        stage="periodic_review",
        stage_turns=STAGE_MAX_TURNS["periodic_review"],
        mode="normal",
        last_input="继续",
        abnormal_turns=5,  # 模拟上一轮异常子模式累积过轮数
    )
    out = await check_stage_node(s)
    assert out["current_stage"] == WorkStage.DAILY_INSPECTION.value
    assert out["stage_turn_count"] == 0
    assert out["abnormal_turn_count"] == 0  # 也归零
    assert out["daily_mode"] == "normal"


@pytest.mark.asyncio
async def test_check_stage_does_not_clear_discharge_order_on_transition():
    """反向断言：check_stage_node 不主动清空 discharge_order。

    设计取舍：`discharge_order` 是 checkpointer 里的"业务指令"字段（不是本次决策的临时输入），
    要由 UI / API 层显式管理生命周期；check_stage 只读不写。如果哪天不小心在流转里
    `discharge_order=None` 清掉它，会让 SaaS 撤销工单失效（一旦下发就连本带利清不掉了）。
    """
    s = _state(
        stage="periodic_review",
        stage_turns=STAGE_MAX_TURNS["periodic_review"],
        mode="normal",
        last_input="继续",
        discharge_order={"order_type": "discharge", "reason": "家属申请"},
    )
    out = await check_stage_node(s)
    assert out["current_stage"] == WorkStage.DECOMMISSION_ASSESSMENT.value
    # check_stage_node 的返回值里不应出现 discharge_order——意味着原值保留在 state
    assert "discharge_order" not in out


