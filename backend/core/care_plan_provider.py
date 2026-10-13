# backend/core/care_plan_provider.py
# 照护计划数据 Provider：把"照护计划与照护历史从哪来"这件事从 Agent 里抽出来。
#
# 为什么要抽这一层（改造前的问题）：
#   load_context 在 DB 查不到照护计划时用**硬编码 mock 计划**兜底（"介助"+5 条固定任务），
#   照护历史更是**无论计划来源如何都用 mock**（两条写死的"晨间护理完成"）。
#   结果是：不管老人实际有没有计划，Agent 都拿一份假计划在推进状态机。
#
# 职责边界（这条是设计核心）：
#   - 照护项目、频次、护理时间段 = **机构业务配置**，属于 SaaS，Agent 不得增删改；
#   - 评估结论 → 护理等级建议、以及异常情况下的处置建议 = AI 的增量；
#   - 每日照护计划的**生成规则**也在业务侧（由业务系统生成），
#     care 只读不造——本地实现只是让 demo 能独立跑，不是第二套业务规则。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)


class CarePlanProviderError(RuntimeError):
    """Provider 取数失败（配置缺失 / 远端不可用）。调用方按"计划缺失"处理，不编数据。"""


@dataclass
class CarePlanBundle:
    """一次取数的结果：计划 + 照护历史 + 来源标记（来源要如实上报，不能糊）。"""

    plan: dict | None = None
    history: list[dict] = field(default_factory=list)
    plan_source: str = "missing"   # service_items / ai_assessment / merged / missing
    data_source: str = "local"     # local / saas


class CarePlanProvider(Protocol):
    """照护计划数据源契约。"""

    name: str

    async def fetch(self, elder_id: str, tenant_id: str, history_limit: int = 8) -> CarePlanBundle:
        ...

    async def ensure_items_for_level(self, elder_id: str, care_level: str,
                                     tenant_id: str) -> list[dict]:
        """按护理等级确保该老人名下有对应的照护服务项，返回生效的服务项。

        这是"评估结论 → 业务配置"的那一步：评估给出护理等级，等级决定该老人
        应做哪些照护项目。**谁负责落地在两种 Provider 下是不同的**——
        本地实现由 care 写自己的库；SaaS 实现只取回"该等级可选什么"，
        实际落地由主系统负责（care 不写主系统的业务数据）。这个差异必须显式，
        否则很容易演变成"AI 系统偷偷改了业务配置"。
        """
        ...

    async def today_plan(self, elder_id: str, tenant_id: str) -> list[dict]:
        """取当天的每日照护计划（含执行状态）。"""
        ...

    async def confirm_plan_item(self, plan_item_id: str, tenant_id: str, *,
                                status: str = "done", carer_id: str = "",
                                carer_name: str = "", actual_care_time: str = "") -> bool:
        """护工执行后回写某项计划的状态（对应业务系统的执行结果回写接口）。"""
        ...


# ──────────────────────────────────────────────────────────────
# 本地实现：读 care 自己的库（demo / 离线可跑）
# ──────────────────────────────────────────────────────────────

class LocalCarePlanProvider:
    """从 care 本地库取照护计划与照护历史。

    计划的优先级：
      ① 老人的照护服务项（elder_service_items，机构配置的业务数据）→ 决定"要做什么、多久做一次"
      ② 评估派生的照护计划（care_plans.plan_content，AI 结论）→ 提供等级建议与注意事项
      两者都有 → merged；只有其一时按其一标注来源；都没有 → plan=None（不编）。
    """

    name = "local"

    async def fetch(self, elder_id: str, tenant_id: str, history_limit: int = 8) -> CarePlanBundle:
        from backend.db.repositories import (
            get_latest_care_plan,
            list_care_history,
            list_service_items,
        )

        items = await list_service_items(elder_id, tenant_id=tenant_id)
        record = await get_latest_care_plan(elder_id, tenant_id=tenant_id)
        ai_content = (record or {}).get("plan_content") or {}

        plan: dict | None = None
        if items and ai_content:
            plan = self._merge(elder_id, items, ai_content, record)
            source = "merged"
        elif items:
            plan = self._from_items(elder_id, items)
            source = "service_items"
        elif ai_content:
            plan = dict(ai_content)
            plan.setdefault("plan_id", (record or {}).get("plan_id", ""))
            plan.setdefault("elder_id", elder_id)
            plan["service_items"] = []
            source = "ai_assessment"
        else:
            source = "missing"

        history = await list_care_history(elder_id, tenant_id=tenant_id, limit=history_limit)
        logger.info("care_plan_provider.local_fetched", elder_id=elder_id,
                    plan_source=source, items=len(items), history=len(history))
        return CarePlanBundle(plan=plan, history=history,
                             plan_source=source, data_source=self.name)

    async def ensure_items_for_level(self, elder_id: str, care_level: str,
                                     tenant_id: str) -> list[dict]:
        """本地实现：把该等级的必选项目写进 care 自己的 elder_service_items。"""
        from backend.db.repositories import bind_service_items_for_level

        return await bind_service_items_for_level(elder_id, care_level, tenant_id=tenant_id)

    async def today_plan(self, elder_id: str, tenant_id: str) -> list[dict]:
        """本地实现：按服务项展开当天计划（业务侧生成规则的本地替身）。"""
        from backend.db.repositories import ensure_daily_plan

        return await ensure_daily_plan(elder_id, tenant_id=tenant_id)

    async def confirm_plan_item(self, plan_item_id: str, tenant_id: str, *,
                                status: str = "done", carer_id: str = "",
                                carer_name: str = "", actual_care_time: str = "") -> bool:
        """本地实现：直接回写 care 自己的 daily_care_plans。"""
        from backend.db.repositories import confirm_daily_plan_item

        return await confirm_daily_plan_item(
            plan_item_id, tenant_id=tenant_id, status=status, carer_id=carer_id,
            carer_name=carer_name, actual_care_time=actual_care_time)

    @staticmethod
    def _tasks_from_items(items: list[dict]) -> list[str]:
        """把服务项渲染成可执行任务行（频次与护理时间段来自业务配置，不由模型编）。"""
        tasks = []
        for it in items:
            name = it.get("project_name") or it.get("project_id") or "照护项目"
            times = it.get("times_per_day") or 0
            freq = it.get("frequency_name") or (f"{times}次/日" if times else "")
            slots = it.get("nurse_time_slots") or []
            window = ""
            if isinstance(slots, list) and slots:
                parts = [f"{s.get('startTime', '')}-{s.get('endTime', '')}"
                         for s in slots if isinstance(s, dict)]
                window = "、".join(p for p in parts if p.strip("-"))
            line = name
            if freq:
                line += f"（{freq}）"
            if window:
                line += f" 时间窗：{window}"
            if not it.get("is_required", True):
                line += " [选做]"
            tasks.append(line)
        return tasks

    def _from_items(self, elder_id: str, items: list[dict]) -> dict:
        return {
            "plan_id": f"items-{elder_id}",
            "elder_id": elder_id,
            "care_level": self._care_level_of(items),
            "daily_tasks": self._tasks_from_items(items),
            "special_notes": "",
            "service_items": items,
        }

    def _merge(self, elder_id: str, items: list[dict], ai_content: dict, record: dict | None) -> dict:
        plan = dict(ai_content)
        plan["plan_id"] = (record or {}).get("plan_id") or f"items-{elder_id}"
        plan["elder_id"] = elder_id
        # 业务配置的服务项覆盖 AI 派生的任务清单：AI 不能决定"该做什么服务"
        plan["daily_tasks"] = self._tasks_from_items(items)
        plan["service_items"] = items
        plan.setdefault("care_level", self._care_level_of(items))
        return plan

    @staticmethod
    def _care_level_of(items: list[dict]) -> str:
        """护理等级取服务项里登记的等级；服务项没带就留空由上游（评估）决定。"""
        for it in items:
            if it.get("care_level"):
                return it["care_level"]
        return ""


# ──────────────────────────────────────────────────────────────
# 机构 SaaS 实现（对接主系统）：
# ──────────────────────────────────────────────────────────────

class SaasCarePlanProvider:
    """从机构业务系统取照护计划与照护历史。

    四个端点路径见下方 `PATH_*` 常量——它们是**占位命名**，请按你自己的后端契约调整。
    需要改动的地方都集中在这一个类里，Agent 与数据层不感知：

      PATH_SERVICE_ITEMS    老人已登记的照护服务项
      PATH_AVAILABLE_ITEMS  某护理等级可选的服务项（用于"从可选里推荐"）
      PATH_DAILY_PLAN       当天照护计划
      PATH_CONFIRM_ITEM     回写执行结果

    字段名同理：请求体字段与响应包裹结构（如 {code, data}）因系统而异，
    解析逻辑集中在 `_parse_*` 与 `_normalize_status` 里并容忍字段缺失，
    换系统只需要改这几个方法，不用动 Agent。
    """

    name = "saas"

    PATH_SERVICE_ITEMS = "/elder/service-items"
    PATH_AVAILABLE_ITEMS = "/elder/available-service-items"
    PATH_DAILY_PLAN = "/care-plan/daily"
    PATH_CONFIRM_ITEM = "/care-plan/confirm"

    def __init__(self, base_url: str, token: str = "", timeout: float = 10.0):
        if not base_url:
            raise CarePlanProviderError("SaaS Provider 未配置 base_url（SAAS_CARE_BASE_URL）")
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _post(self, path: str, payload: dict) -> dict:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._base + path, json=payload, headers=self._headers())
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:
            raise CarePlanProviderError(f"{path} 调用失败: {type(e).__name__}: {e}") from e
        # 主系统统一返回 {code, data, msg}
        return body.get("data", body) if isinstance(body, dict) else {}

    async def fetch(self, elder_id: str, tenant_id: str, history_limit: int = 8) -> CarePlanBundle:
        items = self._parse_items(
            await self._post(self.PATH_SERVICE_ITEMS, {"elderId": elder_id}))
        daily = self._parse_daily(
            await self._post(self.PATH_DAILY_PLAN,
                             {"elderId": elder_id, "limit": history_limit}))

        plan: dict | None = None
        source = "missing"
        if items:
            plan = LocalCarePlanProvider()._from_items(elder_id, items)
            source = "service_items"

        history = [{"date": d.get("planDate") or d.get("nurseTime") or "",
                    "tasks": [d.get("projectName", "")],
                    "abnormal": "" if (d.get("statusF") or d.get("status")) in ("完成", "done", "1") else "未完成"}
                   for d in daily]

        logger.info("care_plan_provider.saas_fetched", elder_id=elder_id,
                    plan_source=source, items=len(items), history=len(history))
        return CarePlanBundle(plan=plan, history=history,
                              plan_source=source, data_source=self.name)

    async def ensure_items_for_level(self, elder_id: str, care_level: str,
                                     tenant_id: str) -> list[dict]:
        """SaaS 实现：只**读取**该护理等级可选的服务内容，不在主系统里写数据。

        返回的是"该等级允许做的项目"；实际给老人挂哪些项目由主系统决定，
        care 用它来做计划渲染与"从可选项里推荐"的输入。
        """
        raw = await self._post(self.PATH_AVAILABLE_ITEMS,
                               {"elderId": elder_id, "nurseLevelName": care_level})
        return self._parse_items(raw)

    async def today_plan(self, elder_id: str, tenant_id: str) -> list[dict]:
        """SaaS 实现：读主系统的当天照护计划（care 不生成）。"""
        raw = await self._post(self.PATH_DAILY_PLAN, {"elderId": elder_id, "today": True})
        return [{
            "plan_item_id": str(r.get("id") or r.get("planId") or ""),
            "project_name": r.get("projectName") or r.get("project_name") or "",
            "nurse_time": r.get("nurseTime") or "",
            "carer_id": str(r.get("carerId") or ""),
            "carer_name": r.get("carerName") or "",
            "actual_care_time": r.get("actualCareTime") or "",
            "status": self._normalize_status(r.get("statusF") or r.get("status")),
        } for r in self._parse_daily(raw)]

    async def confirm_plan_item(self, plan_item_id: str, tenant_id: str, *,
                                status: str = "done", carer_id: str = "",
                                carer_name: str = "", actual_care_time: str = "") -> bool:
        """SaaS 实现：回写主系统的照护计划（PATH_CONFIRM_ITEM）。

        这里 care **会写主系统**，但写的是"执行结果"这一类事实数据（谁在什么时候做了），
        不是业务规则本身——这是与"不重复实现生成规则"并不矛盾的分工。
        """
        await self._post(self.PATH_CONFIRM_ITEM, {
            "id": plan_item_id, "status": status, "carerId": carer_id,
            "careerName": carer_name, "actualCareTime": actual_care_time,
        })
        return True

    @staticmethod
    def _normalize_status(raw) -> str:
        """主系统状态值 → care 的 done/missed/planned。"""
        text = str(raw or "").strip().lower()
        if text in ("done", "finished", "完成", "已完成", "1"):
            return "done"
        if text in ("missed", "未完成", "超时", "2"):
            return "missed"
        return "planned"

    @staticmethod
    def _parse_items(raw) -> list[dict]:
        """老人服务内容 → 统一的 service_items 结构（字段名兼容多种写法）。"""
        rows = raw if isinstance(raw, list) else (raw.get("list") or raw.get("records") or [])
        out = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            out.append({
                "project_id": str(r.get("projectId") or r.get("id") or ""),
                "project_name": r.get("projectName") or r.get("project_name") or "",
                "frequency_id": str(r.get("frequencyId") or ""),
                "frequency_name": r.get("frequencyName") or "",
                "times_per_day": r.get("serverTimes") or r.get("timesPerDay") or 0,
                "nurse_time_slots": r.get("nurseTimeDto") or r.get("nurse_time_slots") or [],
                "is_required": bool(r.get("isRequired", r.get("is_required", True))),
                "care_level": r.get("nurseLevelName") or r.get("care_level") or "",
                "source": "template",
            })
        return out

    @staticmethod
    def _parse_daily(raw) -> list[dict]:
        rows = raw if isinstance(raw, list) else (raw.get("list") or raw.get("records") or [])
        return [r for r in (rows or []) if isinstance(r, dict)]


# ──────────────────────────────────────────────────────────────
# 工厂
# ──────────────────────────────────────────────────────────────

def get_care_plan_provider() -> CarePlanProvider:
    """按配置返回 Provider。默认本地；配了 SAAS_CARE_BASE_URL 才走主系统。

    SaaS 实现初始化失败时**回落本地并告警**，而不是让整条照护流程挂掉——
    取不到业务数据时，Agent 应该报告"计划缺失"，不是崩溃，也不是编一份假的。
    """
    settings = get_settings()
    backend = (getattr(settings, "care_plan_provider", "local") or "local").strip().lower()
    if backend == "saas":
        try:
            return SaasCarePlanProvider(
                base_url=getattr(settings, "saas_care_base_url", ""),
                token=getattr(settings, "saas_care_token", ""),
            )
        except CarePlanProviderError as e:
            logger.error("care_plan_provider.saas_unavailable", error=str(e), fallback="local")
    return LocalCarePlanProvider()
